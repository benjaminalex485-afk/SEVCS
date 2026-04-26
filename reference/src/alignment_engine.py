import cv2
import numpy as np
import time

class AlignmentEngine:
    def __init__(self):
        self.tracks_motion = {} # map track_id -> {'last_center': (x,y), 'last_time': t}

    def detect_motion(self, track_id, current_center):
        """
        Returns True if moved > 5 pixels or new track
        """
        current_time = time.time()
        if track_id not in self.tracks_motion:
            self.tracks_motion[track_id] = {'last_center': current_center, 'last_time': current_time}
            return True
        
        last = self.tracks_motion[track_id]
        dist = np.linalg.norm(np.array(current_center) - np.array(last['last_center']))
        
        # Debounce: Update only if moved significantly or enough time passed (300ms)
        if dist > 5.0 or (current_time - last['last_time']) > 0.3:
            self.tracks_motion[track_id] = {'last_center': current_center, 'last_time': current_time}
            return True
        return False

    def calculate_overlap(self, mask, slot_polygon, frame_wh):
        """
        Calculates overlap ratio between vehicle mask and slot polygon.
        """
        slot_mask = np.zeros((frame_wh[1], frame_wh[0]), dtype=np.uint8)
        cv2.fillPoly(slot_mask, [slot_polygon], 1)
        
        if mask.shape != slot_mask.shape:
             mask = cv2.resize(mask, (frame_wh[0], frame_wh[1]), interpolation=cv2.INTER_NEAREST)

        intersection = np.logical_and(mask, slot_mask)
        overlap_pixels = np.sum(intersection)
        vehicle_pixels = np.sum(mask)
        
        if vehicle_pixels == 0: return 0.0
        return overlap_pixels / vehicle_pixels

    def bboxes_intersect(self, boxA, boxB):
        """
        boxA/B: (x, y, w, h)
        """
        x1, y1, w1, h1 = boxA
        x2, y2, w2, h2 = boxB
        return not (x1 + w1 < x2 or x2 + w2 < x1 or y1 + h1 < y2 or y2 + h2 < y1)

    def evaluate_alignment(self, track_id, mask, centroid, slot, frame_wh, track_age=0, vehicle_box=None):
        """
        Computes Hybrid Alignment Score [0.0, 1.0] and returns features.
        """
        # --- GEOMETRY PREFILTER ---
        if vehicle_box is not None and hasattr(slot, 'bbox'):
             if not self.bboxes_intersect(vehicle_box, slot.bbox):
                 return 0.0, {"status": "SKIPPED_PREFILTER", "overlap": 0.0}

        # 1. Overlap Score
        overlap_ratio = self.calculate_overlap(mask, slot.polygon, frame_wh)
        
        # --- HARD VALIDATION LAYER (RELAXED) ---
        # We now allow scores for any overlap > 40%, below that we still return 0.0
        if overlap_ratio < 0.40:
            return 0.0, {"overlap": overlap_ratio, "status": "REJECTED_MIN_OVERLAP", "track_age": track_age}

        # --- SOFT SCORING LAYER ---
        # Component 1: Overlap (normalized)
        overlap_score = min(1.0, overlap_ratio / 0.95) 
        
        # Component 2: Centroid Score
        M = cv2.moments(slot.polygon)
        if M["m00"] == 0: return 0.0, {}
        slot_cx, slot_cy = int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])
        dist = np.linalg.norm(np.array(centroid) - np.array([slot_cx, slot_cy]))
        centroid_score = max(0.0, 1.0 - (dist / 100.0))
        
        # Component 3: Orientation Score
        orientation_score = 1.0 
        
        # Component 4: Stability Weight (Track Age)
        # 30 frames (~1 sec) to reach full stability score
        stability_weight = min(1.0, track_age / 30.0) 
        
        # Weights
        w_overlap = 0.4
        w_centroid = 0.25
        w_orientation = 0.2
        w_stability = 0.15
        
        final_score = (w_overlap * overlap_score) + \
                      (w_centroid * centroid_score) + \
                      (w_orientation * orientation_score) + \
                      (w_stability * stability_weight)
        
        features = {
            "overlap_ratio": overlap_ratio,
            "centroid_score": centroid_score,
            "orientation_score": orientation_score,
            "stability_weight": stability_weight,
            "final_score": final_score,
            "track_age": track_age
        }
        
        return min(1.0, max(0.0, final_score)), features
