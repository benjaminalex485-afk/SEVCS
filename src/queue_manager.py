import time
import numpy as np
import supervision as sv
from scipy.optimize import linear_sum_assignment
from .priority_engine import PriorityEngine
import logging
from .slot_state_machine import SlotState, SuggestionState
from . import utils

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
    def __init__(self):
        self.queue = {} # track_id -> QueueEntry
        self.priority_engine = PriorityEngine()
        self.entry_stability = {} # track_id -> frames_seen
        self.zone_cache = {} # polygon_hash -> PolygonZone

    def to_dict(self):
        """Deep isolation snapshot: returns primitive types only."""
        return {
            "entries": [e.to_dict() for e in self.queue.values()],
            "count": len(self.queue)
        }

    def get_suggestions_snapshot(self):
        """Returns a list of active suggestions for the UI."""
        from .slot_state_machine import SuggestionState
        suggestions = []
        # We look at the slots to see who has a suggested_track_id
        # We need access to slots, but QueueManager doesn't hold them.
        # Wait! The main loop passes slots to update_suggestions.
        # Maybe we should store the last results?
        # Actually, let's just build it from the queue entries.
        for entry in self.queue.values():
            if entry.assigned_slot is not None:
                suggestions.append({
                    "track_id": int(entry.track_id),
                    "slot_id": int(entry.assigned_slot + 1),
                    "priority": float(entry.priority_score),
                    "state": entry.suggestion_state.name
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
        """
        Global Allocation using Hungarian Algorithm (Linear Sum Assignment).
        Advisory only.
        """
        now = utils.now()
        
        # 1. Filter Suggestable Slots
        suggestable_slots = [
            s for s in slots 
            if s.state == SlotState.FREE and s.locked_track_id is None
        ]
        
        # Filter Queue Entries
        queue_list = sorted(self.queue.values(), key=lambda x: x.track_id)
        
        # GUARD: Clear all slot suggestions if no assignments possible
        if not suggestable_slots or not queue_list:
            for s in slots:
                s.suggested_track_id = None
                s.suggestion_timestamp = 0.0
                s.suggestion_state = SuggestionState.SOFT
                s.suggestion_confidence = 0.0
            for entry in self.queue.values():
                entry.assigned_slot = None
                entry.priority_score = 0.0
            return

        # 2. Build Cost Matrix [Vehicles x Slots]
        num_v = len(queue_list)
        num_s = len(suggestable_slots)
        cost_matrix = np.zeros((num_v, num_s))
        
        for v_idx, entry in enumerate(queue_list):
            for s_idx, slot in enumerate(suggestable_slots):
                priority = self.priority_engine.compute_priority(entry, slot)
                # ISSUE 9: Priority Saturation Logging
                if priority > 0.95:
                    logger.warning(f"[PRIORITY] Saturation Track {entry.track_id} Score={priority:.2f}")
                cost_matrix[v_idx, s_idx] = 1.0 - priority

        # 3. Hungarian Assignment
        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        
        # Mapping for current assignments
        current_assignments = {} # slot_id -> track_id
        for v_idx, s_idx in zip(row_ind, col_ind):
            slot = suggestable_slots[s_idx]
            entry = queue_list[v_idx]
            current_assignments[slot.slot_id] = entry.track_id
            
            # Temporary storage of priority for later application
            entry._temp_priority = 1.0 - cost_matrix[v_idx, s_idx]

        # 4. Apply Trust State Machine & Hysteresis
        for slot in suggestable_slots:
            new_tid = current_assignments.get(slot.slot_id)
            
            # 1. If suggestion changed identity, reset completely
            if new_tid != slot.suggested_track_id:
                if new_tid is not None:
                    logger.info(f"[SUGGEST] Slot {slot.slot_id+1} -> Track {new_tid}")
                slot.suggested_track_id = int(new_tid) if new_tid is not None else None
                slot.suggestion_timestamp = now
                slot.suggestion_state = SuggestionState.SOFT
                slot.suggestion_confidence = 0.0
                continue

            # 2. If suggestion identity is stable, progress trust monotonically
            if slot.suggested_track_id is not None:
                stable_for = now - slot.suggestion_timestamp
                
                # Progress state forward (Never jump backward for same identity)
                if stable_for >= 3.0:
                    slot.suggestion_state = SuggestionState.COMMITTED
                elif stable_for >= 1.0:
                    if slot.suggestion_state == SuggestionState.SOFT:
                        slot.suggestion_state = SuggestionState.STABLE
                
                # Update confidence based on priority
                # Find the entry to get the fresh priority score
                for entry in queue_list:
                    if entry.track_id == slot.suggested_track_id:
                        slot.suggestion_confidence = entry._temp_priority
                        break

        # 5. Finalize QueueEntry fields
        reverse_map = {s.suggested_track_id: int(s.slot_id) for s in slots if s.suggested_track_id is not None}
        for entry in self.queue.values():
            suggested_slot_id = reverse_map.get(entry.track_id)
            if suggested_slot_id is not None:
                entry.assigned_slot = suggested_slot_id
                entry.priority_score = getattr(entry, '_temp_priority', 0.0)
                # Sync suggestion state from slot
                for s in slots:
                    if s.slot_id == suggested_slot_id:
                        entry.suggestion_state = s.suggestion_state
                        break
            else:
                entry.assigned_slot = None
                entry.priority_score = 0.0
                entry.suggestion_state = SuggestionState.SOFT
            
            if hasattr(entry, '_temp_priority'):
                del entry._temp_priority
