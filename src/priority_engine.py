import numpy as np
from . import utils

class PriorityEngine:
    W_BOOKING = 0.5
    W_WAIT = 0.3
    W_DISTANCE = 0.1
    W_TYPE = 0.1
    
    def __init__(self, max_dist=1500.0):
        self.max_dist = max_dist

    def compute_priority(self, vehicle_entry, slot, now=0.0):
        """
        Computes a normalized priority score [0, 1] for a vehicle-slot pair.
        """
        # 1. Booking Score (Binary)
        # Stage 3 Rule: booking_id is only set if Stage 2 binding occurred
        booking_score = 1.0 if vehicle_entry.booking_id is not None else 0.0
        
        # 2. Wait Time Score (Normalized over 120s)
        wait_time = now - vehicle_entry.arrival_time
        wait_score = min(1.0, wait_time / 120.0)
        
        # 3. Distance Score (Normalized by fixed frame diagonal)
        vehicle_pos = vehicle_entry.centroid
        slot_pos = slot.centroid
        dist = np.linalg.norm(np.array(vehicle_pos) - np.array(slot_pos))
        distance_score = max(0.0, 1.0 - (dist / self.max_dist))
        
        # 4. Type Score (Compatibility - Hardcoded for demo)
        type_score = 1.0
        
        # Combined Base Score
        score = (self.W_BOOKING * booking_score + 
                  self.W_WAIT * wait_score + 
                  self.W_DISTANCE * distance_score + 
                  self.W_TYPE * type_score)
        
        # 5. Dynamic Priority Boost
        if wait_time > 120.0:
            score += 0.2
            
        return min(1.0, score)
