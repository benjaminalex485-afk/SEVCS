import time
import numpy as np
import supervision as sv

class QueueManager:
    def __init__(self, timeout=10.0):
        self.timeout = timeout
        # queue: list of dicts {'id': int, 'arrival_time': float, 'last_seen': float}
        self.queue = [] 
        # reservations: dict {slot_index: {'vehicle_id': int, 'timestamp': float}}
        self.reservations = {}
        # entry_stability: map track_id -> count of frames seen in zone
        self.entry_stability = {}

    def update(self, detections, queue_zones):
        """
        Updates the queue based on current tracked detections in the queue zone.
        """
        current_time = time.time()
        
        # 1. Identify vehicles currently in the Queue Zone
        vehicles_in_zone = set()
        
        for zone_poly in queue_zones:
            polygon = np.array(zone_poly, np.int32)
            if polygon.shape[0] < 3: continue

            zone = sv.PolygonZone(
                polygon=polygon, 
                triggering_anchors=[sv.Position.CENTER]
            )
            is_inside = zone.trigger(detections=detections)
            
            if detections.tracker_id is not None:
                ids_in_zone = detections.tracker_id[is_inside]
                vehicles_in_zone.update(ids_in_zone)

        # 2. Add new vehicles to Queue using Stability check
        queue_ids = {v['id'] for v in self.queue}
        
        # Cleanup stability for vehicles no longer in zone
        active_ids = {v_id for v_id in vehicles_in_zone}
        self.entry_stability = {id: count for id, count in self.entry_stability.items() if id in active_ids}

        for v_id in vehicles_in_zone:
            # Increment stability count
            self.entry_stability[v_id] = self.entry_stability.get(v_id, 0) + 1
            
            # Join queue only if seen for > 5 frames (~0.2s)
            if v_id not in queue_ids and self.entry_stability.get(v_id, 0) > 5:
                print(f"Vehicle {v_id} stabilized in zone. Joining queue.")
                self.queue.append({
                    'id': v_id,
                    'arrival_time': current_time,
                    'last_seen': current_time
                })
            
            # Update last seen for existing queue members
            for v in self.queue:
                if v['id'] == v_id:
                    v['last_seen'] = current_time
                    break

        # 3. Prune Queue: Remove if lost from zone AND not reserved
        # Also remove if reservation exists but vehicle is gone for a long time (safety)
        self.queue = [
            v for v in self.queue 
            if (current_time - v['last_seen'] < 2.0) or (self.is_reserved(v['id']) and (current_time - v['last_seen'] < 10.0))
        ]
        
        return len(self.queue)

    def is_reserved(self, vehicle_id):
        for slot_idx, res in self.reservations.items():
            if res['vehicle_id'] == vehicle_id:
                return True
        return False

    def assign_slots(self, free_slots):
        """
        Assigns the next available vehicle to a free slot.
        """
        current_time = time.time()
        
        # Check for slots that are FREE and NOT RESERVED
        available_slots = [
            s for s in free_slots 
            if s not in self.reservations
        ]

        if not available_slots:
            return

        # Assign older unreserved vehicles to available slots
        for v in self.queue:
            # Only assign if not reserved AND seen recently (within 2s to handle low FPS)
            if not self.is_reserved(v['id']):
                time_since_seen = current_time - v['last_seen']
                if time_since_seen < 2.0:
                    if available_slots:
                        slot_to_assign = available_slots.pop(0)
                        self.reservations[slot_to_assign] = {
                            'vehicle_id': v['id'],
                            'timestamp': current_time
                        }
                        print(f"DEBUG: Reserved Slot {slot_to_assign + 1} for Vehicle {v['id']}")
                else:
                    print(f"DEBUG: Vehicle {v['id']} in queue but too stale ({time_since_seen:.1f}s) to assign.")

    def cleanup_reservations(self):
        """
        Timed-out reservations are returned to FREE.
        """
        current_time = time.time()
        expired_slots = []
        for slot_idx, res in self.reservations.items():
            if current_time - res['timestamp'] > self.timeout:
                print(f"Reservation expired for Slot {slot_idx + 1} (Vehicle {res['vehicle_id']})")
                expired_slots.append(slot_idx)
        
        for s in expired_slots:
            del self.reservations[s]

    def manage_reservations(self, slots):
        """
        Synchronizes the queue system with the Slot state machine.
        - Assigns new reservations to FREE slots.
        - Fulfills reservations if a car enters.
        - Cleans up timed-out reservations.
        """
        current_time = time.time()
        from .slot_state_machine import SlotState

        # 1. Cleanup expired reservations
        self.cleanup_reservations()

        # 2. Assign reservations to FREE slots
        # Get indices of slots that are truly FREE (no car, no reservation)
        free_indices = [
            i for i, s in enumerate(slots) 
            if s.state == SlotState.FREE and i not in self.reservations
        ]

        # Try to assign (DISABLED for validation clarity)
        # if free_indices:
        #    self.assign_slots(free_indices)

        # 3. Synchronize Slot Objects with Manager State
        for i, slot in enumerate(slots):
            if i in self.reservations:
                # Manager says it's reserved
                res = self.reservations[i]
                
                if slot.state in [SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                    print(f"Slot {i+1}: Physical reservation fulfilled by Vehicle {slot.locked_track_id}")
                    del self.reservations[i]
                else:
                    slot.set_state(SlotState.RESERVED)
                    slot.reservation_id = res['vehicle_id']
            elif hasattr(slot, 'reservation_id') and slot.reservation_id == -99:
                # This is a Virtual Booking from the API
                # If slot becomes occupied, clear the virtual booking from global state
                if slot.state in [SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                    from main import G_STATE
                    if i in G_STATE['virtual_bookings']:
                        print(f"Slot {i+1}: Virtual booking for {G_STATE['virtual_bookings'][i]} fulfilled.")
                        del G_STATE['virtual_bookings'][i]
            else:
                # Manager says NOT reserved
                if slot.state == SlotState.RESERVED:
                    # Slot thinks it's reserved, but manager cleared it (timeout or error)
                    slot.set_state(SlotState.FREE)
                    slot.reservation_id = None

    def get_slot_status(self, slot_idx, is_occupied_physically):
        """
        Returns (status_text, color)
        """
        # (Kept for backwards compatibility if needed, but main logic now in manage_reservations)
        if is_occupied_physically:
            return "Occupied", (0, 0, 255)
        if slot_idx in self.reservations:
            vid = self.reservations[slot_idx]['vehicle_id']
            return f"Res: ID {vid}", (0, 255, 255)
        return "Free", (0, 255, 0)
