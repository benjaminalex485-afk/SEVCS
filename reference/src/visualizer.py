import cv2
import numpy as np
import time
import supervision as sv
from .slot_state_machine import SlotState

class Visualizer:
    def __init__(self, sidebar_w=200):
        self.sidebar_w = sidebar_w
        self.box_annotator = sv.BoxAnnotator()
        self.label_annotator = sv.LabelAnnotator()
        self.mask_annotator = sv.MaskAnnotator()

    def draw_overlays(self, frame, slots, queue_zones, slot_estimates):
        """
        Draws slot polygons, queue zones, and status text.
        """
        overlay = frame.copy()
        current_time = time.time()
        
        for i, slot in enumerate(slots):
            # 1. State-to-Color Logic
            color = (0, 255, 0) # FREE (default)
            text = "FREE"
            
            is_charging_allowed = slot.enable_charging()
            
            if slot.state == SlotState.RESERVED:
                color = (0, 255, 255) # Yellow
                text = f"RES #{slot.reservation_id}"
            elif slot.state == SlotState.ALIGNMENT_PENDING:
                color = (255, 165, 0) # Orange
                text = "ALIGNING..."
            elif slot.state == SlotState.CHARGING:
                if is_charging_allowed:
                    color = (0, 255, 0) # Bright Green
                    text = "CHARGING"
                else:
                    color = (0, 165, 255) # Warning - Needs adjustment
                    text = "CHK ALIGN"
                
                # Check Estimates
                if i in slot_estimates:
                    remaining = slot_estimates[i] - current_time
                    if remaining > 0:
                        minutes = int(remaining // 60)
                        text = f"Rem: {minutes}m"
                        color = (128, 0, 128) # Purple
                    else:
                        text = "DONE"
            
            elif slot.state == SlotState.MISALIGNED:
                # Blink logic for bad parking
                if int(current_time * 4) % 2 == 0:
                    color = (0, 0, 255)
                else:
                    color = (0, 0, 128)
                text = "BAD PARK"
            
            # 2. Render Polygons
            cv2.polylines(overlay, [slot.polygon], True, color, 2)
            cv2.fillPoly(overlay, [slot.polygon], color)
            
            # 3. Label Placement
            M = cv2.moments(slot.polygon)
            if M["m00"] != 0:
                cX, cY = int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])
                cv2.putText(overlay, text, (cX-40, cY), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
                
                # Show precision metrics
                if slot.state in [SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                    score_txt = f"{int(slot.smoothed_alignment_score * 100)}%"
                    cv2.putText(overlay, score_txt, (cX-20, cY+20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)

        # 4. Draw Queue Zones
        for q_poly in queue_zones:
            cv2.polylines(overlay, [np.array(q_poly, np.int32)], True, (255, 0, 0), 2)

        # 5. Alpha Blend
        cv2.addWeighted(overlay, 0.4, frame, 0.6, 0, frame)
        return frame

    def draw_detections(self, frame, detections):
        """
        Draws YOLO bounding boxes and tracker IDs.
        """
        labels = [f"#{tid}" for tid in detections.tracker_id] if detections.tracker_id is not None else []
        frame = self.box_annotator.annotate(scene=frame, detections=detections)
        frame = self.label_annotator.annotate(scene=frame, detections=detections, labels=labels)
        return frame

    def draw_sidebar(self, frame, queue_manager):
        """
        Renders the right-hand sidebar with controls, legend, and diagnostics.
        """
        # Create Border
        frame = cv2.copyMakeBorder(frame, 0, 0, 0, self.sidebar_w, cv2.BORDER_CONSTANT, value=(40, 40, 40))
        sb_x = frame.shape[1] - self.sidebar_w + 10
        
        # 1. Controls
        cv2.putText(frame, "CONTROLS", (sb_x, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.line(frame, (sb_x, 35), (sb_x + 180, 35), (100, 100, 100), 1)
        
        controls = [
            ("'s'", "Select Slots"),
            ("'z'", "Select Queue"),
            ("'1-9'", "Set Estimate"),
            ("'c'", "Clear All"),
            ("'q'", "Quit / Save")
        ]
        for i, (key, desc) in enumerate(controls):
            iy = 60 + (i * 25)
            cv2.putText(frame, f"{key}: {desc}", (sb_x, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

        # 2. Legend
        legend_y = 200
        cv2.putText(frame, "LEGEND", (sb_x, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.line(frame, (sb_x, legend_y + 5), (sb_x + 180, legend_y + 5), (100, 100, 100), 1)
        
        items = [
            ((0, 255, 0), "FREE / CHARGED"),
            ((0, 255, 255), "RESERVED"),
            ((255, 165, 0), "ALIGNING"),
            ((0, 165, 255), "CHK ALIGN"),
            ((0, 0, 255), "MISALIGNED"),
            ((128, 0, 128), "ESTIMATE SET"),
            ((255, 0, 0), "QUEUE ZONE")
        ]
        for i, (color, label) in enumerate(items):
            iy = legend_y + 30 + (i * 22)
            cv2.rectangle(frame, (sb_x, iy - 12), (sb_x + 15, iy + 3), color, -1)
            cv2.putText(frame, label, (sb_x + 25, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

        # 3. Queue Monitor
        queue_y = legend_y + 190
        cv2.putText(frame, "QUEUE MONITOR", (sb_x, queue_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.line(frame, (sb_x, queue_y + 5), (sb_x + 180, queue_y + 5), (100, 100, 100), 1)
        
        if not queue_manager.queue:
            cv2.putText(frame, "Waiting: 0", (sb_x, queue_y + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
        else:
            for idx, v in enumerate(queue_manager.queue):
                iy = queue_y + 30 + (idx * 18)
                q_text = f"ID {v['id']} (Queue)"
                if queue_manager.is_reserved(v['id']):
                    q_text = f"ID {v['id']} (RES)"
                cv2.putText(frame, q_text, (sb_x, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

        # Stability Candidates
        cand_y = queue_y + 70
        offset = 0
        for v_id, count in queue_manager.entry_stability.items():
            if not any(v['id'] == v_id for v in queue_manager.queue):
                iy = cand_y + 15 + (offset * 15)
                cv2.putText(frame, f"Entry ID {v_id}: {count}/5", (sb_x, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 200, 255), 1)
                offset += 1

        return frame
