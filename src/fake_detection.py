import numpy as np
import time
import yaml
import os
import cv2
import supervision as sv
import random

class ScenarioEngine:
    def __init__(self, scenario_name="walk_in", frame_wh=(1280, 720)):
        self.scenario_name = scenario_name
        self.frame_wh = frame_wh
        self.start_time = time.monotonic()
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
                        cx, cy = int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])
                        self.centroids.append((cx, cy))
                    else:
                        self.centroids.append(tuple(np.mean(poly, axis=0).astype(int)))

    def reset(self): self.start_time = time.monotonic()

    def is_complete(self):
        elapsed = time.monotonic() - self.start_time
        durations = {
            "walk_in": 120, "occlusion_id_shift": 120, "misaligned_forever": 120, "slot_jumper": 120,
            "rapid_entry": 120, "long_occlusion": 120, "conflict_test": 120, "stage2_happy_path": 120,
            "stage2_id_shift": 120, "stage2_expiry": 120, "stage2_race": 120, "stage2_equal_timing": 120,
            "stage2_borderline": 120, "stage2_drift": 120
        }
        return elapsed > durations.get(self.scenario_name, 30)

    def get_detections(self):
        elapsed = time.monotonic() - self.start_time
        if self.scenario_name == "walk_in": return self._scenario_walk_in(elapsed)
        elif self.scenario_name == "occlusion_id_shift": return self._scenario_occlusion_id_shift(elapsed)
        elif self.scenario_name == "misaligned_forever": return self._scenario_misaligned_forever(elapsed)
        elif self.scenario_name == "slot_jumper": return self._scenario_slot_jumper(elapsed)
        elif self.scenario_name == "rapid_entry": return self._scenario_rapid_entry(elapsed)
        elif self.scenario_name == "long_occlusion": return self._scenario_long_occlusion(elapsed)
        elif self.scenario_name == "conflict_test": return self._scenario_conflict_test(elapsed)
        elif self.scenario_name == "stage2_happy_path": return self._scenario_stage2_happy_path(elapsed)
        elif self.scenario_name == "stage2_id_shift": return self._scenario_stage2_id_shift(elapsed)
        elif self.scenario_name == "stage2_expiry": return self._scenario_stage2_expiry(elapsed)
        elif self.scenario_name == "stage2_race": return self._scenario_stage2_race(elapsed)
        elif self.scenario_name == "stage2_equal_timing": return self._scenario_stage2_equal_timing(elapsed)
        elif self.scenario_name == "stage2_borderline": return self._scenario_stage2_borderline(elapsed)
        elif self.scenario_name == "stage2_drift": return self._scenario_stage2_drift(elapsed)
        return self._empty()

    def _empty(self):
        return sv.Detections(xyxy=np.empty((0, 4)), tracker_id=np.array([], dtype=int), class_id=np.array([], dtype=int), mask=np.empty((0, self.frame_wh[1], self.frame_wh[0]), dtype=bool))

    def _create_detections(self, tracker_ids, masks):
        boxes = []
        for mask in masks:
            pos = np.where(mask)
            if len(pos[0]) > 0: boxes.append([np.min(pos[1]), np.min(pos[0]), np.max(pos[1]), np.max(pos[0])])
            else: boxes.append([0, 0, 0, 0])
        return sv.Detections(xyxy=np.array(boxes, dtype=np.float32), tracker_id=np.array(tracker_ids, dtype=int), class_id=np.array([2] * len(tracker_ids), dtype=int), mask=np.array(masks, dtype=bool))

    def _generate_controlled_mask(self, slot_idx, target_overlap, jitter=True):
        """
        Refined 2D mask generator with jitter cap (5px).
        """
        slot_poly = self.slots[slot_idx]
        slot_mask = np.zeros((self.frame_wh[1], self.frame_wh[0]), dtype=np.uint8)
        cv2.fillPoly(slot_mask, [slot_poly], 1)
        slot_area = np.sum(slot_mask)
        
        # Iterative 2D search for target overlap
        best_mask = slot_mask.copy()
        current_best_diff = 1.0
        
        # We try different x, y shifts to find target overlap (Centered range)
        for dx in range(-150, 150, 5):
            for dy in range(-50, 50, 5):
                shifted = np.zeros_like(slot_mask)
                sy_start, sy_end = max(0, dy), min(self.frame_wh[1], self.frame_wh[1] + dy)
                sx_start, sx_end = max(0, dx), min(self.frame_wh[0], self.frame_wh[0] + dx)
                shifted[sy_start:sy_end, sx_start:sx_end] = slot_mask[max(0, -dy):min(self.frame_wh[1], self.frame_wh[1] - dy), max(0, -dx):min(self.frame_wh[0], self.frame_wh[0] - dx)]
                
                overlap = np.sum(np.logical_and(shifted, slot_mask)) / slot_area
                if abs(overlap - target_overlap) < current_best_diff:
                    current_best_diff = abs(overlap - target_overlap)
                    best_mask = shifted
                if current_best_diff < 0.02: break
            if current_best_diff < 0.02: break
        
        if jitter:
            # Apply tiny jitter (capped at 5px)
            jx, jy = random.randint(-5, 5), random.randint(-5, 5)
            jittered = np.zeros_like(best_mask)
            sy_s, sy_e = max(0, jy), min(self.frame_wh[1], self.frame_wh[1] + jy)
            sx_s, sx_e = max(0, jx), min(self.frame_wh[0], self.frame_wh[0] + jx)
            jittered[sy_s:sy_e, sx_s:sx_e] = best_mask[max(0, -jy):min(self.frame_wh[1], self.frame_wh[1] - jy), max(0, -jx):min(self.frame_wh[0], self.frame_wh[0] - jx)]
            return jittered > 0
            
        return best_mask > 0

    # --- SCENARIOS ---
    def _scenario_stage2_happy_path(self, t):
        if t < 5: return self._empty()
        if t < 15: return self._create_detections([1], [self._generate_controlled_mask(0, 0.4 + (t-5)/10 * 0.55)])
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.98)])

    def _scenario_stage2_borderline(self, t):
        """
        Stay exactly at 0.73 overlap to test score guard.
        """
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.73)])

    def _scenario_stage2_drift(self, t):
        """
        Align -> AUTH_PENDING -> Drift to 0.5
        """
        if t < 15: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.5)])

    def _scenario_stage2_id_shift(self, t):
        if t < 10: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 25: return self._create_detections([2], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_stage2_expiry(self, t):
        if t < 70: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_stage2_race(self, t):
        if t < 10: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_stage2_equal_timing(self, t):
        if t < 10: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 11: return self._empty()
        if t < 20: return self._create_detections([2], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_walk_in(self, t):
        if t < 15: return self._create_detections([1], [self._generate_controlled_mask(0, min(0.95, t/15))])
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.98)])

    def _scenario_occlusion_id_shift(self, t):
        if t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 10: return self._empty()
        return self._create_detections([2], [self._generate_controlled_mask(0, 0.95)])

    def _scenario_misaligned_forever(self, t):
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.35)])

    def _scenario_slot_jumper(self, t):
        if t < 10: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._create_detections([1], [self._generate_controlled_mask(1, 0.95)])

    def _scenario_rapid_entry(self, t):
        if 2 < t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        return self._empty()

    def _scenario_long_occlusion(self, t):
        if t < 5: return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])
        if t < 18: return self._empty()
        return self._create_detections([1], [self._generate_controlled_mask(0, 0.95)])

    def _scenario_conflict_test(self, t):
        mask1 = self._generate_controlled_mask(0, 0.95)
        mask2 = self._generate_controlled_mask(1 if len(self.slots) > 1 else 0, 0.5)
        return self._create_detections([1, 2], [mask1, mask2])
