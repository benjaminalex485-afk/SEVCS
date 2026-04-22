import numpy as np
import time
import yaml
import os
import cv2
import supervision as sv

class ScenarioEngine:
    def __init__(self, scenario_name="walk_in", frame_wh=(1280, 720)):
        self.scenario_name = scenario_name
        self.frame_wh = frame_wh
        self.start_time = time.time()
        self.slots = []
        self.centroids = []
        self._load_config()

    def _load_config(self):
        config_path = "config.yaml"
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
                self.slots = [np.array(poly, np.int32) for poly in config.get('slots', [])]
                for poly in self.slots:
                    M = cv2.moments(poly)
                    if M["m00"] != 0:
                        cx = int(M["m10"] / M["m00"])
                        cy = int(M["m01"] / M["m00"])
                        self.centroids.append((cx, cy))
                    else:
                        self.centroids.append(tuple(np.mean(poly, axis=0).astype(int)))

    def reset(self):
        self.start_time = time.time()

    def is_complete(self):
        elapsed = time.time() - self.start_time
        durations = {
            "walk_in": 30,
            "occlusion_id_shift": 25,
            "misaligned_forever": 30,
            "slot_jumper": 25,
            "rapid_entry": 10,
            "long_occlusion": 40,
            "conflict_test": 20
        }
        return elapsed > durations.get(self.scenario_name, 30)

    def get_detections(self):
        elapsed = time.time() - self.start_time
        
        if self.scenario_name == "walk_in":
            return self._scenario_walk_in(elapsed)
        elif self.scenario_name == "occlusion_id_shift":
            return self._scenario_occlusion_id_shift(elapsed)
        elif self.scenario_name == "misaligned_forever":
            return self._scenario_misaligned_forever(elapsed)
        elif self.scenario_name == "slot_jumper":
            return self._scenario_slot_jumper(elapsed)
        elif self.scenario_name == "rapid_entry":
            return self._scenario_rapid_entry(elapsed)
        elif self.scenario_name == "long_occlusion":
            return self._scenario_long_occlusion(elapsed)
        elif self.scenario_name == "conflict_test":
            return self._scenario_conflict_test(elapsed)
        return self._empty()

    def _empty(self):
        return sv.Detections(
            xyxy=np.empty((0, 4)),
            tracker_id=np.array([], dtype=int),
            class_id=np.array([], dtype=int),
            mask=np.empty((0, self.frame_wh[1], self.frame_wh[0]), dtype=bool)
        )

    def _create_detections(self, tracker_ids, masks):
        boxes = []
        for mask in masks:
            pos = np.where(mask)
            if len(pos[0]) > 0:
                boxes.append([np.min(pos[1]), np.min(pos[0]), np.max(pos[1]), np.max(pos[0])])
            else:
                boxes.append([0, 0, 0, 0])
        
        return sv.Detections(
            xyxy=np.array(boxes, dtype=np.float32),
            tracker_id=np.array(tracker_ids, dtype=int),
            class_id=np.array([2] * len(tracker_ids), dtype=int),
            mask=np.array(masks, dtype=bool)
        )

    def _generate_controlled_mask(self, slot_idx, target_overlap):
        """
        Iteratively adjusts mask position to hit target_overlap.
        """
        slot_poly = self.slots[slot_idx]
        slot_mask = np.zeros((self.frame_wh[1], self.frame_wh[0]), dtype=np.uint8)
        cv2.fillPoly(slot_mask, [slot_poly], 1)
        slot_area = np.sum(slot_mask)
        
        # Start with a mask identical to the slot
        mask = slot_mask.copy()
        
        if target_overlap >= 0.99:
            return mask > 0
        
        # Simple iterative shift to reduce overlap
        # Shift mask to the right until overlap matches target
        shift_x = 0
        max_shift = 500
        epsilon = 0.02
        
        for i in range(max_shift):
            shifted_mask = np.zeros_like(slot_mask)
            if shift_x < self.frame_wh[0]:
                shifted_mask[:, shift_x:] = mask[:, :self.frame_wh[0]-shift_x]
            
            overlap_area = np.sum(np.logical_and(shifted_mask, slot_mask))
            current_overlap = overlap_area / slot_area
            
            if current_overlap <= target_overlap + epsilon:
                return shifted_mask > 0
            
            shift_x += 2
            
        return shifted_mask > 0

    def _scenario_walk_in(self, t):
        if t < 2: return self._empty()
        
        # Phase 1: Entry (t=2 to t=8) -> Overlap 0.0 to 0.4
        if t < 8:
            overlap = (t - 2) / 6 * 0.4
            return self._create_detections([1], [self._generate_controlled_mask(0, overlap)])
        
        # Phase 2: Alignment (t=8 to t=15) -> Overlap 0.4 to 0.95
        if t < 15:
            overlap = 0.4 + (t - 8) / 7 * 0.55
            return self._create_detections([1], [self._generate_controlled_mask(0, overlap)])
        
        # Phase 3: Charging (t=15 to t=25) -> Perfect alignment
        if t < 25:
            return self._create_detections([1], [self._generate_controlled_mask(0, 0.98)])
            
        return self._empty()

    def _scenario_occlusion_id_shift(self, t):
        if t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 10: return self._empty()
        # Returns with new ID
        if t < 20: return self._create_detections([2], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_misaligned_forever(self, t):
        if t < 2: return self._empty()
        # Stay at 35% overlap (below the 40% threshold in alignment_engine)
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.35)])

    def _scenario_slot_jumper(self, t):
        if t < 10: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        # Jumps to Slot 2 with same ID
        if t < 20: return self._create_detections([1], [self._generate_controlled_mask(1, 0.95)])
        return self._empty()

    def _scenario_rapid_entry(self, t):
        if 2 < t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_long_occlusion(self, t):
        if t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 18: return self._empty() # 13s occlusion > 10s limit
        if t < 30: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_conflict_test(self, t):
        """
        Two vehicles, one placed exactly between two slots to test Hungarian assignment.
        """
        if t < 2: return self._empty()
        
        # Car 1: Perfect in Slot 1
        mask1 = self._generate_controlled_mask(0, 0.95)
        # Car 2: Placed between Slot 2 and 3 (if Slot 3 exists, else just offset Slot 2)
        target_slot = 1 if len(self.slots) > 1 else 0
        mask2 = self._generate_controlled_mask(target_slot, 0.5) # Misaligned but competing
        
        return self._create_detections([1, 2], [mask1, mask2])
