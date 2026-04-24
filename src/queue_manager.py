import time
import numpy as np
import supervision as sv
from scipy.optimize import linear_sum_assignment
from .priority_engine import PriorityEngine
import logging
from .slot_state_machine import SlotState, SuggestionState
from .industrial_utils import ReasonCode, SystemMode, EVENT_BUS, SignalQuality, MIN_MOVEMENT, DRIFT_THRESHOLD, SIGNAL_SMOOTHING
from . import utils
import collections

logger = logging.getLogger(__name__)

class QueueEntry:
    def __init__(self, track_id, centroid=(0,0), arrival_time=None):
        self.track_id = track_id
        self.global_id = f"v_{track_id}_{int(arrival_time or 0)}"
        self.arrival_time = arrival_time or 0.0
        self.last_seen_time = arrival_time or 0.0
        self.centroid = centroid
        self.user = None
        self.booking_id = None
        self.priority_score = 0.0
        self.assigned_slot = None
        self.suggestion_state = SuggestionState.SOFT
        self.last_assignment_time = 0.0
        
        # Stage 4.5 Reality Layer
        self.history = collections.deque(maxlen=20) # (time, centroid, bbox)
        self.latest_conf = 0.0
        self.drift_counter = 0.0
        self.drift_score = 0.0
        self.smoothed_signal_conf = 0.5 # Real-world cold start bias
        self.decision_reason = "INIT"

    def to_dict(self):
        """Pure & Deterministic projection of entry state."""
        return {
            "global_id": self.global_id,
            "track_id": int(self.track_id),
            "arrival_time": float(self.arrival_time),
            "last_seen_time": float(self.last_seen_time),
            "priority": float(self.priority_score),
            "assigned_slot": self.assigned_slot,
            "signal_confidence": float(self.smoothed_signal_conf),
            "drift_score": float(self.drift_score),
            "decision_reason": str(self.decision_reason)
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
        self.last_update_time = utils.system_now(caller="main_loop")
        self.anomaly_window_start = utils.system_now(caller="main_loop")
        self.has_recent_anomaly = False
        
        # State consistency tracking
        self.consistency_counters = {} # (slot_id, track_id) -> count
        self.hold_timers = {} # slot_id -> (track_id, start_time, confidence)

    def to_dict(self):
        """Pure & Deterministic projection of manager state."""
        # Sorting for forensic stability (outside live state)
        sorted_entries = sorted(self.queue.values(), key=lambda x: x.global_id)
        return {
            "queue": [e.to_dict() for e in sorted_entries],
            "system_health": float(self.system_health),
            "thrash_rate": float(self.thrash_rate)
        }

    def _update_system_health(self, has_anomaly, dt):
        """Asymmetric health: fast drop, slow recovery with accelerated jump."""
        if has_anomaly:
            self.system_health = max(0.0, self.system_health - 0.2)
            self.anomaly_window_start = now
            self.has_recent_anomaly = True
        else:
            # Slow recovery (EWMA-like)
            recovery_rate = 0.05 * dt
            self.system_health = min(1.0, self.system_health + recovery_rate)
            
            # Accelerated Jump: 5s clean + good signals
            if now - self.anomaly_window_start > 5.0:
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

    def _prune_queue(self, max_tracks):
        """STRICT Triple-Key Deterministic Pruning: (last_seen, global_id, track_id)"""
        if len(self.queue) <= max_tracks:
            return
            
        # 1. Sort by deterministic triple-key
        items = list(self.queue.values())
        items.sort(key=lambda x: (x.last_seen_time, x.global_id, x.track_id))
        
        # 2. Prune oldest N
        num_to_remove = len(self.queue) - max_tracks
        removed_ids = [item.track_id for item in items[:num_to_remove]]
        
        for tid in removed_ids:
            logger.warning(f"[PRUNE] Evicting Track {tid} (Deterministic Load Shedding)")
            del self.queue[tid]
            if tid in self.entry_stability:
                del self.entry_stability[tid]

    def update_queue(self, detections, queue_zones, frame_time=0.0):
        """
        Updates the queue based on vehicles in the QUEUE_ZONE.
        """
        now = frame_time
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
            # Update stability counter
            self.entry_stability[tid] = self.entry_stability.get(tid, 0) + 1
            
            # Find detection data for this tid
            idx_list = np.where(detections.tracker_id == tid)[0]
            if len(idx_list) == 0: continue
            idx = idx_list[0]
            bbox = detections.xyxy[idx]
            conf = detections.confidence[idx] if detections.confidence is not None else 0.0

            if tid not in self.queue:
                # Only add if stabilized (> 5 frames)
                if self.entry_stability[tid] > 5:
                    logger.info(f"[QUEUE] {EVENT_BUS.next_id()} Vehicle {tid} ENTERED at {centroid}")
                    self.queue[tid] = QueueEntry(tid, centroid, arrival_time=now)
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
                    entry.latest_conf = conf
                    entry.history.append((now, centroid, bbox))

        # 3. Cleanup Stale Entries (Leak Prevention & ID Reuse Handling)
        to_remove = []
        for tid, entry in self.queue.items():
            if now - entry.last_seen_time > 2.0:
                logger.info(f"[QUEUE] REMOVE Track {tid} (Stale)")
                to_remove.append(tid)
        
        for tid in to_remove:
            if tid in self.queue: del self.queue[tid]
            if tid in self.entry_stability: del self.entry_stability[tid]
            
        # 4. Deterministic Pruning (Load Shedding Guard)
        import os
        MAX_TRACKS = int(os.getenv("SEVCS_MAX_TRACKS", 50))
        self._prune_queue(MAX_TRACKS)

    def update_suggestions(self, slots, allow_new_assignments=True, frame_time=0.0):
        """Hardened production-grade suggestion engine with industrial reliability."""
        now = frame_time
        dt = now - self.last_update_time
        self.last_update_time = now

        # --- STAGE 4.5: COLD START SUPPRESSION ---
        if not allow_new_assignments:
            # Update holds for slots but don't perform new assignments
            for s in slots:
                s.update_hold(now=now)
            return
        
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
                priority = self.priority_engine.compute_priority(entry, slot, now=now)
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

            # --- STAGE 4.5: SIGNAL QUALITY LAYER ---
            stability = SignalQuality.compute_stability(entry.history)
            consistency = SignalQuality.compute_consistency(entry.history)
            
            # Composite raw signal confidence
            raw_signal_conf = (0.4 * stability + 0.3 * consistency + 0.3 * entry.latest_conf)
            
            # EWMA Debouncing
            entry.smoothed_signal_conf = (SIGNAL_SMOOTHING * raw_signal_conf + 
                                         (1.0 - SIGNAL_SMOOTHING) * entry.smoothed_signal_conf)
            global_conf = np.clip(entry.smoothed_signal_conf, 0.0, 1.0)
            
            # --- STAGE 4.5: TRUTH DRIFT DETECTOR ---
            if len(entry.history) >= 2:
                prev_pos = np.array(entry.history[-2][1])
                curr_pos = np.array(entry.history[-1][1])
                motion_vec = curr_pos - prev_pos
                
                if np.linalg.norm(motion_vec) >= MIN_MOVEMENT:
                    slot_vec = np.array(slot.centroid) - curr_pos
                    
                    norm_motion = SignalQuality.normalize_safe(motion_vec)
                    norm_slot = SignalQuality.normalize_safe(slot_vec)
                    
                    alignment = np.dot(norm_motion, norm_slot)
                    
                    if alignment < -0.3: # Moving AWAY from slot
                        entry.drift_counter += dt
                    else:
                        entry.drift_counter = 0
                else:
                    # Reset counter if movement is below threshold to prevent accumulation during jitter
                    entry.drift_counter = 0
                
                # Clamped Drift Scoring
                entry.drift_score = np.clip(entry.drift_counter / DRIFT_THRESHOLD, 0.0, 1.0)
                
                # Reduce confidence based on drift
                global_conf *= (1.0 - 0.5 * entry.drift_score)

            decision_conf, reason = self._get_decision_confidence(global_conf, norm_margin)
            
            # --- STRUCTURED EXPLAINABILITY ---
            flags = []
            if norm_margin > 0.8: flags.append("HIGH_MARGIN")
            if (now - slot.suggestion_timestamp > 1.0): flags.append("STABLE")
            if self.system_health > 0.8: flags.append("HEALTHY")
            if entry.drift_score > 0.3: flags.append("DRIFT_WARNING")
            if global_conf < 0.4: flags.append("LOW_SIGNAL")
            
            entry.decision_reason = " + ".join(sorted(flags)) if flags else "EVALUATING"
            entry.decision_confidence = decision_conf # Store for snapshot
            
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

        # --- STAGE 4.5: LEAK PREVENTION ---
        # Cleanup stale consistency counters (only keep active assignments)
        active_keys = {(s.slot_id, s.suggested_track_id) for s in slots if s.suggested_track_id is not None}
        self.consistency_counters = {k: v for k, v in self.consistency_counters.items() if k in active_keys}

        # 4. Sync Entry Fields
        for entry in self.queue.values():
            entry.assigned_slot = None
            for s in slots:
                if s.suggested_track_id == entry.track_id:
                    entry.assigned_slot = s.slot_id
                    entry.priority_score = s.suggestion_confidence
                    entry.suggestion_state = s.suggestion_state
                    break
