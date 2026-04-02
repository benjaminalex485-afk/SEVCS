import cv2
import numpy as np
import supervision as sv
import time
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

# Professional Package-style Imports
from src.detector import SlotDetector
from src.queue_manager import QueueManager
from src.slot_state_machine import Slot, SlotState, AlignmentState
from src.alignment_engine import AlignmentEngine
from src.visualizer import Visualizer
from src import utils

# --- API SERVER CONFIG ---
api_app = Flask(__name__)
CORS(api_app)

# Shared state for API
G_STATE = {
    'slots': [],
    'estimates': {},
    'queue_manager': None,
    'virtual_bookings': {} # slot_idx -> username
}

@api_app.route('/api/vision/status', methods=['GET'])
def get_vision_status():
    slots_data = []
    charging_count = 0
    reserved_count = 0
    
    for i, s in enumerate(G_STATE['slots']):
        is_charging = (s.state.name == 'CHARGING')
        is_reserved = (s.state.name == 'RESERVED' or i in G_STATE['virtual_bookings'])
        
        if is_charging: charging_count += 1
        if is_reserved: reserved_count += 1
        
        slots_data.append({
            'id': i + 1,
            'state': s.state.name,
            'reserved_for': G_STATE['virtual_bookings'].get(i, None),
            'estimate': G_STATE['estimates'].get(i, None)
        })
        
    return jsonify({
        'charging_count': charging_count,
        'reserved_count': reserved_count,
        'queue_count': len(G_STATE['queue_manager'].queue) if G_STATE['queue_manager'] else 0,
        'slots': slots_data
    })

@api_app.route('/api/vision/book', methods=['POST'])
def book_slot():
    data = request.json or {}
    username = data.get('username', 'Anonymous')
    kwh = data.get('kwh', 20.0)
    charge_type = data.get('type', 'Standard')
    
    # Rate mapping
    rates = {'Standard': 7.0, 'Fast': 50.0, 'Ultra': 150.0}
    rate = rates.get(charge_type, 7.0)
    
    # 1. Look for FREE slots
    assigned_idx = -1
    for i, s in enumerate(G_STATE['slots']):
        if s.state.name == 'FREE' and i not in G_STATE['virtual_bookings']:
            assigned_idx = i
            break
            
    # 2. If no FREE, find soonest to finish
    if assigned_idx == -1:
        best_time = float('inf')
        for i, est in G_STATE['estimates'].items():
            if est < best_time:
                best_time = est
                assigned_idx = i
                
    if assigned_idx == -1 and G_STATE['slots']:
        assigned_idx = 0 # Default fallback
        
    if assigned_idx != -1:
        G_STATE['virtual_bookings'][assigned_idx] = username
        # Update estimate for the new user (after current one finishes)
        current_est = G_STATE['estimates'].get(assigned_idx, time.time())
        wait_time = (kwh / rate) * 3600
        G_STATE['estimates'][assigned_idx] = max(current_est, time.time()) + wait_time
        
        return jsonify({
            'status': 'success',
            'slot_id': assigned_idx + 1,
            'estimate': G_STATE['estimates'][assigned_idx]
        })
        
    return jsonify({'status': 'error', 'message': 'No slots available'}), 400

def run_api():
    api_app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

def main():
    # Load configuration
    config = utils.load_config()
    camera_index = config.get('camera_index', 0)
    model_path = config.get('model_path', 'models/yolov8n-seg.pt')
    class_ids = config.get('class_ids', [2, 3, 5, 7])
    
    # Initialize Capture
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print("Error: Could not open camera.")
        return
    
    resolution_wh = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    # Initialize Modules
    print(f"Loading model {model_path}...")
    detector = SlotDetector(model_path, class_ids)
    tracker = sv.ByteTrack()
    queue_manager = QueueManager(timeout=10.0)
    alignment_engine = AlignmentEngine()
    visualizer = Visualizer()
    
    # State Management
    slots = [Slot(i, poly) for i, poly in enumerate(config.get('slots', []))]
    slot_estimates = {} # Initialize here to avoid UnboundLocalError
    
    # Sync with API Global State
    G_STATE['slots'] = slots
    G_STATE['queue_manager'] = queue_manager
    G_STATE['estimates'] = slot_estimates
    
    # Start API Thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    print("Vision API Server started on port 5001")

    queue_zones = config.get('queue_zones', [])
    conf_threshold = config.get('conf_threshold', 0.15)
    track_ages = {} 
    
    print("System Ready.")
    
    while True:
        ret, frame = cap.read()
        if not ret: break
        
        # 1. Detect & Track
        detections = detector.detect(frame, conf=conf_threshold)
        detections = tracker.update_with_detections(detections)
        
        # Update Track Ages
        current_track_ids = detections.tracker_id if detections.tracker_id is not None else []
        for tid in current_track_ids:
            track_ages[tid] = track_ages.get(tid, 0) + 1
        
        # Cleanup old tracks
        track_ages = {tid: age for tid, age in track_ages.items() if tid in current_track_ids}

        # 2. Logic per Slot
        for i, slot in enumerate(slots):
            # Track state for this specific slot
            vehicle_track_id = None
            vehicle_center = None
            vehicle_mask = None
            vehicle_bbox = None
            
            # Find the best candidate vehicle for this slot
            best_overlap = 0.0
            best_d_idx = -1

            for d_idx in range(len(detections)):
                tid = detections.tracker_id[d_idx]
                box = detections.xyxy[d_idx]
                mask = detections.mask[d_idx] if detections.mask is not None else None
                center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
                
                is_inside = cv2.pointPolygonTest(slot.polygon, center, False) >= 0
                overlap_ratio = 0.0
                if mask is not None:
                    overlap_ratio = alignment_engine.calculate_overlap(mask, slot.polygon, resolution_wh)
                
                is_this_reserved_car = (slot.reservation_id == tid)
                entry_threshold = 0.01 if not is_this_reserved_car else 0.001

                if is_inside or overlap_ratio > entry_threshold:
                    if overlap_ratio > best_overlap:
                        best_overlap = overlap_ratio
                        best_d_idx = d_idx

            if best_d_idx != -1:
                tid = detections.tracker_id[best_d_idx]
                box = detections.xyxy[best_d_idx]
                vehicle_track_id = tid
                vehicle_center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
                vehicle_mask = detections.mask[best_d_idx] if detections.mask is not None else None
                vehicle_bbox = (int(box[0]), int(box[1]), int(box[2]-box[0]), int(box[3]-box[1]))

            # Update State
            if vehicle_track_id is not None:
                slot.track_age = track_ages.get(vehicle_track_id, 0)
                if slot.state == SlotState.FREE or slot.state == SlotState.RESERVED:
                    slot.set_state(SlotState.ALIGNMENT_PENDING)
                    slot.locked_track_id = vehicle_track_id
                elif slot.state in [SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                    if slot.locked_track_id == vehicle_track_id:
                        has_moved = alignment_engine.detect_motion(vehicle_track_id, vehicle_center)
                        if has_moved or (time.time() - slot.last_evaluation_time > 0.3):
                            if vehicle_mask is not None:
                                score, features = alignment_engine.evaluate_alignment(
                                    vehicle_track_id, vehicle_mask, vehicle_center, 
                                    slot, resolution_wh, slot.track_age, vehicle_bbox
                                )
                                slot.update_alignment(score, features)
                                slot.last_evaluation_time = time.time()
                            
                            if slot.alignment_state == AlignmentState.ALIGNED:
                                slot.set_state(SlotState.CHARGING)
                            elif slot.alignment_state == AlignmentState.MISALIGNED:
                                slot.set_state(SlotState.MISALIGNED)
                slot.handle_occlusion(False)
            else:
                if slot.state != SlotState.FREE and slot.state != SlotState.RESERVED:
                    slot.handle_occlusion(True)
        
        # 3. Queue Logic
        queue_manager.update(detections, queue_zones)
        
        # Apply virtual bookings to Slot behavior
        for i, slot in enumerate(slots):
            if i in G_STATE['virtual_bookings'] and slot.state == SlotState.FREE:
                slot.set_state(SlotState.RESERVED)
                slot.reservation_id = -99 # Virtual reservation marker

        queue_manager.manage_reservations(slots)

        # 4. Rendering
        frame = visualizer.draw_overlays(frame, slots, queue_zones, slot_estimates)
        frame = visualizer.draw_detections(frame, detections)
        frame = visualizer.draw_sidebar(frame, queue_manager)

        cv2.imshow("Smart EV Charging", frame)
        key = cv2.waitKey(1) & 0xFF

        # Controls
        if key == ord('q'):
            break
        elif key == ord('s'):
            print("Entering Slot Selection Mode...")
            new_slots = utils.select_zones(cap, "Select Charging Slots", current_zones=config.get('slots', []), mode="slot")
            if new_slots:
                config['slots'] = new_slots
                utils.save_config(config)
                slots = [Slot(i, poly) for i, poly in enumerate(new_slots)]
                print(f"Saved {len(new_slots)} slots.")
        elif key == ord('z'):
            print("Entering Queue Zone Selection Mode...")
            new_zones = utils.select_zones(cap, "Select Queue Zones", current_zones=config.get('queue_zones', []), mode="queue")
            if new_zones:
                config['queue_zones'] = new_zones
                utils.save_config(config)
                queue_zones = new_zones
                print(f"Saved {len(new_zones)} queue zones.")
        elif key == ord('c'):
            print("Clearing all configurations...")
            config['slots'] = []
            config['queue_zones'] = []
            utils.save_config(config)
            slots = []
            queue_zones = []
        elif 49 <= key <= 57: 
            slot_idx = key - 49 
            if slot_idx < len(slots):
                kwh, rate = utils.get_charging_input(slot_idx + 1)
                if kwh and rate and rate > 0:
                     hours = kwh / rate
                     slot_estimates[slot_idx] = time.time() + (hours * 3600)
                     print(f"Estimate set for Slot {slot_idx+1}: {hours:.2f} hours")
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
