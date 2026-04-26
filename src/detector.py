import cv2
from ultralytics import YOLO
import supervision as sv
import numpy as np

from . import utils

class SlotDetector:
    def __init__(self, model_path, class_ids):
        self.model = YOLO(model_path)
        self.class_ids = class_ids
        self.slot_scores = [] # List to track score [0.0, 1.0] for each slot
        self.DEV_MODE = utils.DEV_MODE

    def detect(self, frame, conf=0.25):
        """
        Runs YOLOv8 inference on the frame.
        Returns: supervision.Detections object
        """
        if self.DEV_MODE:
            conf = 0.1 # Lower confidence in DEV MODE
        results = self.model(frame, verbose=False, conf=conf)[0]
        detections = sv.Detections.from_ultralytics(results)
        
        # Filter by class_id
        if self.class_ids:
             detections = detections[np.isin(detections.class_id, self.class_ids)]
        
        return detections

    def check_occupancy(self, detections, slots, frame_resolution_wh):
        """
        Checks which slots are occupied based on detections using Hybrid Scoring.
        Score Update: +0.2 if present, -0.1 if absent.
        Threshold: Occupied if score > 0.7.
        Returns: List of booleans (True if occupied)
        """
        # Resize scores list if slots changed
        if len(self.slot_scores) != len(slots):
            self.slot_scores = [0.0] * len(slots)

        occupancy_status = []
        
        for i, slot in enumerate(slots):
            polygon = np.array(slot, np.int32)
            if polygon.shape[0] < 3:
                occupancy_status.append(False)
                continue
            
            zone = sv.PolygonZone(
                polygon=polygon, 
                triggering_anchors=[sv.Position.CENTER]
            )
            
            # Check instantaneous presence
            is_present = zone.trigger(detections=detections)
            instant_detection = bool(np.any(is_present))
            
            # Update Score
            if instant_detection:
                self.slot_scores[i] += 0.2
            else:
                self.slot_scores[i] -= 0.1
            
            # Clamp Score
            self.slot_scores[i] = max(0.0, min(1.0, self.slot_scores[i]))
            
            # Determine Status
            occupancy_threshold = 0.3 if self.DEV_MODE else 0.7
            is_occupied = self.slot_scores[i] > occupancy_threshold
            if self.DEV_MODE and is_occupied and self.slot_scores[i] <= 0.7:
                logger.info(f"[DEV MODE] Low confidence occupancy accepted for Slot {i+1}: {self.slot_scores[i]:.2f}")
            occupancy_status.append(is_occupied)
        
        return occupancy_status

    def count_in_zones(self, detections, zones):
        """
        Counts unique detections in each zone.
        Returns: Integer count of total vehicles in all zones (sum)
        """
        total_count = 0
        for zone_poly in zones:
            polygon = np.array(zone_poly, np.int32)
            if polygon.shape[0] < 3: continue

            zone = sv.PolygonZone(
                polygon=polygon, 
                triggering_anchors=[sv.Position.CENTER]
            )
            is_inside = zone.trigger(detections=detections)
            total_count += np.sum(is_inside)
        
        return int(total_count)
