import time
import numpy as np
import supervision as sv
from scipy.optimize import linear_sum_assignment
from .priority_engine import PriorityEngine
import logging
from .slot_state_machine import SlotState

logger = logging.getLogger(__name__)

class QueueEntry:
    def __init__(self, track_id, centroid=(0,0)):
        self.track_id = track_id
        self.arrival_time = time.monotonic()
        self.last_seen_time = time.monotonic()
        self.centroid = centroid
        self.user = None
        self.booking_id = None # Bound during Stage 2 flow
        self.priority_score = 0.0
        self.assigned_slot = None
        self.last_assignment_time = 0.0 # for hysteresis

class QueueManager:
    def __init__(self):
        self.queue = {} # track_id -> QueueEntry
        self.priority_engine = PriorityEngine()
        self.entry_stability = {} # track_id -> frames_seen
        self.zone_cache = {} # polygon_hash -> PolygonZone

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
        now = time.monotonic()
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
        now = time.monotonic()
        
        # 1. Filter Suggestable Slots
        suggestable_slots = [
            s for s in slots 
            if s.state == SlotState.FREE and s.locked_track_id is None
        ]
        
        # Filter Queue Entries
        queue_list = sorted(self.queue.values(), key=lambda x: x.track_id)
        
        # GUARD: Clear all slot suggestions if no assignments possible (Issue 3)
        if not suggestable_slots or not queue_list:
            for s in slots:
                s.suggested_track_id = None
                s.suggestion_timestamp = 0.0
            for entry in self.queue.values():
                entry.assigned_slot = None
                entry.priority_score = 0.0
            return

        # Reset suggestions for non-suggestable slots
        for s in slots:
            if s not in suggestable_slots:
                s.suggested_track_id = None
                s.suggestion_timestamp = 0.0

        # 2. Build Cost Matrix [Vehicles x Slots]
        num_v = len(queue_list)
        num_s = len(suggestable_slots)
        cost_matrix = np.zeros((num_v, num_s))
        
        for v_idx, entry in enumerate(queue_list):
            for s_idx, slot in enumerate(suggestable_slots):
                priority = self.priority_engine.compute_priority(entry, slot)
                cost_matrix[v_idx, s_idx] = 1.0 - priority

        # 3. Hungarian Assignment
        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        
        if num_v > num_s:
            logger.info(f"[SUGGEST] Mismatch: {num_v} vehicles vs {num_s} slots. Some unassigned.")

        # Mapping for current assignments
        current_assignments = {} # slot_id -> track_id
        for v_idx, s_idx in zip(row_ind, col_ind):
            slot = suggestable_slots[s_idx]
            entry = queue_list[v_idx]
            current_assignments[slot.slot_id] = entry.track_id
            
            # Temporary storage of priority for later application
            entry._temp_priority = 1.0 - cost_matrix[v_idx, s_idx]

        # 4. Apply Hysteresis & Symmetry
        for slot in suggestable_slots:
            new_tid = current_assignments.get(slot.slot_id)
            
            # If we had a suggestion
            if slot.suggested_track_id is not None:
                # Break if vehicle lost
                if slot.suggested_track_id not in self.queue:
                    logger.info(f"[HYSTERESIS] CLEAR Slot {slot.slot_id+1} (Vehicle Lost)")
                    slot.suggested_track_id = None
                    slot.suggestion_timestamp = 0.0
                # SMART HYSTERESIS: Only hold if still optimal (Issue 2 fix: Refresh timestamp)
                elif now - slot.suggestion_timestamp < 3.0:
                    if new_tid == slot.suggested_track_id:
                        slot.suggestion_timestamp = now # Refresh lock for sticky optimal match
                        continue 
            
            # Update suggestion
            if new_tid != slot.suggested_track_id:
                if new_tid is not None:
                    logger.info(f"[SUGGEST] Slot {slot.slot_id+1} -> Track {new_tid}")
                
                slot.suggested_track_id = new_tid
                slot.suggestion_timestamp = now

        # Update QueueEntry fields based on final slot state (Issue 4: Reverse Map Optimization)
        # 1. Create O(1) Reverse Map
        reverse_map = {s.suggested_track_id: int(s.slot_id) for s in slots if s.suggested_track_id is not None}
        
        # 2. Apply to Entries (Issue 1: temp_priority cleanup)
        for entry in self.queue.values():
            suggested_slot_id = reverse_map.get(entry.track_id)
            
            if suggested_slot_id is not None:
                entry.assigned_slot = suggested_slot_id
                # Apply priority score if assigned
                entry.priority_score = getattr(entry, '_temp_priority', 0.0)
            else:
                entry.assigned_slot = None
                entry.priority_score = 0.0
            
            # Cleanup temp priority to prevent memory/state leak
            if hasattr(entry, '_temp_priority'):
                del entry._temp_priority
