import time
import numpy as np
import supervision as sv
from scipy.optimize import linear_sum_assignment
from .priority_engine import PriorityEngine
import logging
from .slot_state_machine import SlotState, SuggestionState
from .industrial_utils import ReasonCode, SystemMode, EVENT_BUS
from . import utils
import collections

logger = logging.getLogger(__name__)

class QueueEntry:
    def __init__(self, track_id, centroid=(0,0)):
        self.track_id = track_id
        self.arrival_time = utils.now()
        self.last_seen_time = utils.now()
        self.centroid = centroid
        self.user = None
        self.booking_id = None
        self.priority_score = 0.0
        self.assigned_slot = None
        self.suggestion_state = SuggestionState.SOFT
        self.last_assignment_time = 0.0

    def to_dict(self):
        """Deep isolation snapshot: returns primitive types only."""
        return {
            "track_id": int(self.track_id),
            "arrival_time": float(self.arrival_time),
            "user": self.user,
            "priority": float(self.priority_score),
            "assigned_slot": self.assigned_slot,
            "wait_time": float(utils.now() - self.arrival_time)
        }

class QueueManager:
    def __init__(self, max_dist=1500.0):
        self.queue = {} # track_id -> QueueEntry
        self.priority_engine = PriorityEngine(max_dist)
        self.entry_stability = {} # track_id -> frames_seen
        self.zone_cache = {} # polygon_hash -> PolygonZone
        
        # --- Stage 4.1 Hardening ---
        self.system_health = 1.0
        self.thrash_rate = 0.0 # EWMA
        self.system_mode = SystemMode.FULL
        self.mode_reason = ReasonCode.NONE
        self.last_update_time = utils.now()
        self.anomaly_window_start = utils.now()
        self.has_recent_anomaly = False
        
        # State consistency tracking
        self.consistency_counters = {} # (slot_id, track_id) -> count
        self.hold_timers = {} # slot_id -> (track_id, start_time, confidence)

    def to_dict(self):
        """Deep isolation snapshot: returns primitive types only."""
        return {
            "entries": [e.to_dict() for e in self.queue.values()],
            "count": len(self.queue)
        }

    def _update_system_health(self, has_anomaly, dt):
        """Asymmetric health: fast drop, slow recovery with accelerated jump."""
        if has_anomaly:
            self.system_health = max(0.0, self.system_health - 0.2)
            self.anomaly_window_start = utils.now()
            self.has_recent_anomaly = True
        else:
            # Slow recovery (EWMA-like)
            recovery_rate = 0.05 * dt
            self.system_health = min(1.0, self.system_health + recovery_rate)
            
            # Accelerated Jump: 5s clean + good signals
            if utils.now() - self.anomaly_window_start > 5.0:
                self.system_health = 1.0
                self.has_recent_anomaly = False
        
        # Adaptive severity: 1.0 is healthy, 0.0 is catastrophic
        # thrash_rate could also be included here
        self.severity_score = 1.0 - self.system_health 
        self.adaptive_weight = 1.0 - self.severity_score

    def _get_decision_confidence(self, global_conf, margin_conf):
        """Weighted confidence fusion with tiered floor guards."""
        # 1. Hard Floor Guards
        if global_conf < 0.2 or self.system_health < 0.2:
            return 0.0, ReasonCode.LOW_CONFIDENCE
        
        # 2. Tiered Gate (0.2-0.3 conditional)
        if global_conf < 0.3 and margin_conf < 0.8:
            return 0.0, ReasonCode.LOW_CONFIDENCE

        # 3. Weighted Fusion
        # 0.5*global + 0.3*margin + 0.2*health
        score = (0.5 * global_conf) + (0.3 * margin_conf) + (0.2 * self.system_health)
        
        # Adaptive Scaling for SOFT_SAFE mode
        # If health is low, the adaptive_weight will naturally reduce the score
        return score * self.adaptive_weight, ReasonCode.NONE

    def get_suggestions_snapshot(self):
        """Returns a list of active suggestions for the UI."""
        from .slot_state_machine import SuggestionState
        suggestions = []
        for entry in self.queue.values():
            if entry.assigned_slot is not None:
                suggestions.append({
                    "track_id": int(entry.track_id),
                    "slot_id": int(entry.assigned_slot + 1),
                    "priority": float(entry.priority_score),
                    "state": entry.suggestion_state.name,
                    "confidence": getattr(entry, 'decision_confidence', 0.0)
                })
        return suggestions

    def _get_zone(self, polygon_coords):
        poly_hash = hash(tuple(map(tuple, polygon_coords)))
        if poly_hash not in self.zone_cache:
            polygon = np.array(polygon_coords, np.int32)
            self.zone_cache[poly_hash] = sv.PolygonZone(
                polygon=polygon, 
                triggering_anchors=[sv.Position.CENTER]
            )
        return self.zone_cache[poly_hash]

    def update_queue(self, detections, queue_zones):
        """
        Updates the queue based on vehicles in the QUEUE_ZONE.
        """
        now = utils.now()
        vehicles_in_zone = {} # track_id -> centroid
        
        # 1. Detect vehicles in Queue Zones (Efficiency: Use cached zones)
        for zone_poly in queue_zones:
            zone = self._get_zone(zone_poly)
            is_inside = zone.trigger(detections=detections)
            
            if detections.tracker_id is not None:
                ids = detections.tracker_id[is_inside]
                xyxy = detections.xyxy[is_inside]
                for i, tid in enumerate(ids):
                    # Calculate centroid from bbox
                    bbox = xyxy[i]
                    centroid = (int((bbox[0] + bbox[2])/2), int((bbox[1] + bbox[3])/2))
                    vehicles_in_zone[tid] = centroid

        # 2. Add/Update entries (Correction: Strict Reset for Stability)
        # Reset stability for any ID not detected in zone this frame
        active_tids = set(vehicles_in_zone.keys())
        all_tracked_tids = set(self.entry_stability.keys())
        for tid in all_tracked_tids - active_tids:
             # Only reset if NOT in queue (if in queue, we rely on cleanup heartbeat)
             if tid not in self.queue:
                 del self.entry_stability[tid]

        for tid, centroid in vehicles_in_zone.items():
            if tid not in self.queue:
                self.entry_stability[tid] = self.entry_stability.get(tid, 0) + 1
                
                # Only add if stabilized (> 5 frames)
                if self.entry_stability[tid] > 5:
                    logger.info(f"[QUEUE] ADD Track {tid}")
                    self.queue[tid] = QueueEntry(tid, centroid)
            else:
                # Update existing
                entry = self.queue[tid]
                
                # ISSUE 3: Teleportation Guard (ID Reuse Reset)
                dist = np.linalg.norm(np.array(centroid) - np.array(entry.centroid))
                if dist > 300: # Significant jump -> likely ID reuse for new user
                    logger.info(f"[QUEUE] Resetting Track {tid} (ID Reuse / Teleport Detected)")
                    self.queue[tid] = QueueEntry(tid, centroid)
                else:
                    entry.last_seen_time = now
                    entry.centroid = centroid

        # 3. Cleanup Stale Entries (Leak Prevention & ID Reuse Handling)
        to_remove = []
        for tid, entry in self.queue.items():
            if now - entry.last_seen_time > 2.0:
                logger.info(f"[QUEUE] REMOVE Track {tid} (Stale)")
                to_remove.append(tid)
        
        for tid in to_remove:
            if tid in self.queue: del self.queue[tid]
            if tid in self.entry_stability: del self.entry_stability[tid]

    def update_suggestions(self, slots):
        """Hardened production-grade suggestion engine with industrial reliability."""
        now = utils.now()
        dt = now - self.last_update_time
        self.last_update_time = now
        
        # 1. Update System Health & Mode Awareness
        self._update_system_health(self.has_recent_anomaly, dt)
        
        # Filter Suggestable Slots
        suggestable_slots = [
            s for s in slots 
            if s.state == SlotState.FREE and s.locked_track_id is None
        ]
        queue_list = sorted(self.queue.values(), key=lambda x: x.track_id)
        
        # GUARD: Clean reset if nothing to suggest
        if not suggestable_slots or not queue_list:
            for s in slots:
                s.suggested_track_id = None
                s.suggestion_timestamp = 0.0
                s.suggestion_confidence = 0.0
                s.update_hold()
            return

        # 2. Assignment Matrix [Hungarian]
        num_v = len(queue_list)
        num_s = len(suggestable_slots)
        cost_matrix = np.zeros((num_v, num_s))
        for v_idx, entry in enumerate(queue_list):
            for s_idx, slot in enumerate(suggestable_slots):
                # Priority Engine (Stage 3.5: Geometry only by default)
                priority = self.priority_engine.compute_priority(entry, slot)
                # Ensure Geometric Dominance (Intelligence boost handled elsewhere or integrated)
                cost_matrix[v_idx, s_idx] = 1.0 - priority

        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        
        # 3. Decision Logic per Slot
        new_assignments = {suggestable_slots[s_idx].slot_id: queue_list[v_idx] 
                           for v_idx, s_idx in zip(row_ind, col_ind)}
        
        for slot in suggestable_slots:
            entry = new_assignments.get(slot.slot_id)
            if not entry:
                slot.suggested_track_id = None
                slot.update_hold()
                continue

            # --- HARDENING LAYER ---
            v_idx = queue_list.index(entry)
            s_idx = suggestable_slots.index(slot)
            current_score = 1.0 - cost_matrix[v_idx, s_idx]
            
            # Calculate Margin (best - second best)
            row = cost_matrix[v_idx, :]
            sorted_costs = np.sort(row)
            margin = (sorted_costs[1] - sorted_costs[0]) if len(sorted_costs) > 1 else 1.0
            norm_margin = margin / (current_score + 1e-6) if current_score >= 0.2 else 0.0
            
            # Global Confidence (for now just signal quality, could include others)
            global_conf = 1.0 # TODO: Integrate sensor-level confidence
            
            decision_conf, reason = self._get_decision_confidence(global_conf, norm_margin)
            
            # --- CONSISTENCY & HYSTERESIS ---
            candidate_key = (slot.slot_id, entry.track_id)
            if slot.suggested_track_id != entry.track_id:
                # Reset consistency on candidate change
                self.consistency_counters[candidate_key] = 0
                
                # Check for Decaying Hysteresis override
                # If we were previously rejected, require much higher margin
                hysteresis_threshold = max(0.3, 0.85 - 0.1 * (now - slot.suggestion_timestamp))
                if norm_margin < hysteresis_threshold:
                    # HOLD previous state or reject
                    reason = ReasonCode.LOW_CONFIDENCE
                else:
                    # Fresh candidate is good enough to attempt
                    pass
            else:
                self.consistency_counters[candidate_key] = min(100, self.consistency_counters.get(candidate_key, 0) + 1)

            # Time-aware consistency (max(100ms, 2 frames))
            # At 30fps, 100ms is ~3 frames.
            is_consistent = self.consistency_counters.get(candidate_key, 0) >= 3
            
            if decision_conf > 0.5 and is_consistent:
                # ACCEPTED Decision
                if slot.suggested_track_id != entry.track_id:
                    logger.info(f"[SUGGEST] {EVENT_BUS.next_id()} Slot {slot.slot_id+1} -> Track {entry.track_id} (conf={decision_conf:.2f}, margin={norm_margin:.2f})")
                    slot.suggested_track_id = entry.track_id
                    slot.suggestion_timestamp = now
                
                slot.suggestion_confidence = decision_conf
                slot.suggestion_state = SuggestionState.STABLE if (now - slot.suggestion_timestamp > 1.0) else SuggestionState.SOFT
                if (now - slot.suggestion_timestamp > 3.0): slot.suggestion_state = SuggestionState.COMMITTED
            else:
                # REJECTED or HOLD
                if slot.suggested_track_id is not None:
                    # Initiate/Update HOLD state
                    if slot.hold_track_id is None:
                        slot.hold_track_id = slot.suggested_track_id
                        slot.hold_start_time = now
                        slot.hold_confidence = slot.suggestion_confidence
                        slot.hold_frames = 0
                    
                    slot.update_hold()
                    
                    if slot.hold_track_id is None: # Just expired
                        # Fallback Contract: Geometry -> Clear
                        slot.suggested_track_id = None
                        logger.info(f"[SUGGEST] {EVENT_BUS.next_id()} Slot {slot.slot_id+1} -> CLEAR (Reason: {reason.name})")

        # 4. Sync Entry Fields
        for entry in self.queue.values():
            entry.assigned_slot = None
            for s in slots:
                if s.suggested_track_id == entry.track_id:
                    entry.assigned_slot = s.slot_id
                    entry.priority_score = s.suggestion_confidence
                    entry.suggestion_state = s.suggestion_state
                    break
