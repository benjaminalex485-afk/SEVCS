import cv2
import numpy as np
import supervision as sv
import time
import threading
import logging
import os
import copy
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from scipy.optimize import linear_sum_assignment

# Professional Package-style Imports
from src.detector import SlotDetector
from src.queue_manager import QueueManager
from src.slot_state_machine import Slot, SlotState, AlignmentState
from src.alignment_engine import AlignmentEngine
from src.visualizer import Visualizer
from src import utils

# --- LOGGING CONFIG ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("sevcs_events.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- STRUCTURED SYSTEM STATE ---
class SystemState:
    def __init__(self):
        # Granular Locks
        self.vision_lock = threading.Lock()
        self.booking_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.queue_lock = threading.Lock()
        
        # State Data
        self.slots = []             # List of Slot objects
        self.bookings = {}          # slot_idx -> {username, kwh, type, timestamp}
        self.sessions = {}          # slot_idx -> {battery_pct, power, energy, start_time}
        self.queue_manager = None   # QueueManager instance
        self.last_vision_heartbeat = time.time()
        self.camera_online = False
        
        self.users_db = [
            {"username": "admin", "password": "admin", "role": "admin"},
            {"username": "user", "password": "user", "role": "user"}
        ]

G_STATE = SystemState()

# --- ATOMIC SNAPSHOT LAYER ---
def get_system_snapshot():
    """
    Creates a thread-safe snapshot by copying VALUES, not references.
    Independent locks are used sequentially to prevent deadlocks.
    """
    snapshot = {
        "sys": {
            "timestamp": time.time()
        },
        "slots": [],
        "queue": []
    }
    
    # 1. Capture Vision State (Value-based)
    slots_copy = []
    with G_STATE.vision_lock:
        snapshot["sys"]["camera_online"] = G_STATE.camera_online
        snapshot["sys"]["vision_heartbeat"] = G_STATE.last_vision_heartbeat
        for i, slot in enumerate(G_STATE.slots):
            slots_copy.append({
                "id": i + 1,
                "state": slot.state.name,
                "alignment": slot.alignment_state.name,
                "type": "Standard",
                "state_enter_time": slot.state_enter_time
            })
        
    # 2. Capture Bookings
    with G_STATE.booking_lock:
        bookings_copy = copy.deepcopy(G_STATE.bookings)
        
    # 3. Capture Sessions
    with G_STATE.session_lock:
        sessions_copy = copy.deepcopy(G_STATE.sessions)
        
    # 4. Capture Queue
    with G_STATE.queue_lock:
        if G_STATE.queue_manager:
            for i, v in enumerate(G_STATE.queue_manager.queue):
                snapshot["queue"].append({
                    "track_id": v["id"],
                    "position": i + 1,
                    "arrival": v["arrival_time"]
                })
    
    # 5. Assemble Snapshot
    for i, slot_data in enumerate(slots_copy):
        slot_data["booking"] = bookings_copy.get(i)
        slot_data["session"] = sessions_copy.get(i)
        snapshot["slots"].append(slot_data)
                
    snapshot["sys"]["queue_count"] = len(snapshot["queue"])
    snapshot["sys"]["charging_count"] = sum(1 for s in snapshot["slots"] if s["state"] == "CHARGING")
    
    return snapshot

# --- API SERVER CONFIG ---
api_app = Flask(__name__, static_folder='ev_charging_sim/data')
CORS(api_app)

@api_app.route('/')
def serve_index():
    return send_from_directory(api_app.static_folder, 'index.html')

@api_app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(api_app.static_folder, path)

@api_app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    u = data.get('username')
    p = data.get('password')
    for user in G_STATE.users_db:
        if user["username"] == u and user["password"] == p:
             return jsonify({"status": "success", "role": user["role"]})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401

@api_app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify(get_system_snapshot())

@api_app.route('/api/book', methods=['POST'])
def book_slot_api():
    data = request.json or {}
    username = data.get('username', 'Anonymous')
    kwh = data.get('kwh', 20.0)
    charge_type = data.get('type', 'Standard')
    
    snapshot = get_system_snapshot()
    
    with G_STATE.booking_lock:
        assigned_idx = -1
        for i, slot_data in enumerate(snapshot["slots"]):
            if slot_data["state"] == "FREE" and i not in G_STATE.bookings:
                assigned_idx = i
                break
                
        if assigned_idx != -1:
            G_STATE.bookings[assigned_idx] = {
                "user": username,
                "kwh": kwh,
                "type": charge_type,
                "timestamp": time.time()
            }
            logger.info(f"[API] Booking REGISTERED: Slot {assigned_idx+1} for {username}")
            return jsonify({
                "status": "success",
                "slot_id": assigned_idx + 1
            })
            
    return jsonify({"status": "error", "message": "No slots available"}), 400

# --- CHARGING SIMULATION THREAD ---
def charging_simulation_loop():
    logger.info("Charging Simulation Thread started.")
    while True:
        time.sleep(1.0)
        now = time.time()
        
        if now - G_STATE.last_vision_heartbeat > 5.0: # Increased grace period for simulation
            continue
            
        slot_states = []
        with G_STATE.vision_lock:
            for i, slot in enumerate(G_STATE.slots):
                slot_states.append({
                    "idx": i,
                    "state": slot.state,
                    "enter_time": slot.state_enter_time
                })
        
        with G_STATE.session_lock:
            for item in slot_states:
                i = item["idx"]
                state = item["state"]
                enter_time = item["enter_time"]
                
                if state == SlotState.CHARGING:
                    if i not in G_STATE.sessions:
                        if now - enter_time > 2.0:
                            G_STATE.sessions[i] = {
                                "battery_pct": 20,
                                "power": 7.2,
                                "energy": 0.0,
                                "start_time": now
                            }
                            logger.info(f"[SIM] Session STARTED: Slot {i+1}")
                    elif i in G_STATE.sessions:
                        sess = G_STATE.sessions[i]
                        sess["battery_pct"] = min(100, sess["battery_pct"] + 1)
                        sess["energy"] += (sess["power"] / 3600.0)
                        
                elif state == SlotState.FREE:
                    if i in G_STATE.sessions:
                        if now - enter_time > 5.0:
                            del G_STATE.sessions[i]
                            logger.info(f"[SIM] Session CLEANUP: Slot {i+1}")

# --- SIMULATION CONFIG ---
USE_FAKE_INPUT = True
ACTIVE_SCENARIO = "misaligned_forever"

if USE_FAKE_INPUT:
    from src.fake_detection import ScenarioEngine
    engine = ScenarioEngine(ACTIVE_SCENARIO)

def run_api():
    api_app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

def get_centroid(polygon):
    M = cv2.moments(polygon)
    if M["m00"] == 0:
        return np.mean(polygon, axis=0)
    return (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))

def main():
    config = utils.load_config()
    camera_index = config.get('camera_index', 0)
    model_path = config.get('model_path', 'models/yolov8n-seg.pt')
    class_ids = config.get('class_ids', [2, 3, 5, 7])
    
    cap = None
    if not USE_FAKE_INPUT:
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            logger.error("Could not open camera.")
            return
    
    resolution_wh = (1280, 720)
    if not USE_FAKE_INPUT:
        resolution_wh = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        
    detector = SlotDetector(model_path, class_ids)
    tracker = sv.ByteTrack()
    queue_manager = QueueManager(timeout=10.0)
    alignment_engine = AlignmentEngine()
    visualizer = Visualizer()
    
    slots = [Slot(i, poly) for i, poly in enumerate(config.get('slots', []))]
    
    with G_STATE.vision_lock:
        G_STATE.slots = slots
        G_STATE.queue_manager = queue_manager
        G_STATE.camera_online = True
    
    threading.Thread(target=run_api, daemon=True).start()
    threading.Thread(target=charging_simulation_loop, daemon=True).start()
    
    queue_zones = config.get('queue_zones', [])
    conf_threshold = config.get('conf_threshold', 0.15)
    track_ages = {} 
    
    logger.info(f"System Ready. Mode: {'FAKE' if USE_FAKE_INPUT else 'REAL'} | Scenario: {ACTIVE_SCENARIO if USE_FAKE_INPUT else 'N/A'}")
    
    max_diag = np.linalg.norm(resolution_wh)
    
    while True:
        if USE_FAKE_INPUT:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            detections = engine.get_detections()
            time.sleep(0.033) # ~30 FPS
            if engine.is_complete():
                logger.info(f"Scenario {ACTIVE_SCENARIO} complete. Resetting...")
                engine.reset()
        else:
            ret, frame = cap.read()
            if not ret: break
            detections = detector.detect(frame, conf=conf_threshold)
            detections = tracker.update_with_detections(detections)
        
        # Runtime Validation
        if len(detections) > 0 and detections.mask is not None:
            assert detections.mask.shape[1:] == (resolution_wh[1], resolution_wh[0]), f"Mask resolution mismatch! Got {detections.mask.shape[1:]}, expected {(resolution_wh[1], resolution_wh[0])}"

        with G_STATE.vision_lock:
            G_STATE.last_vision_heartbeat = time.time()
        
        current_track_ids = detections.tracker_id if detections.tracker_id is not None else []
        for tid in current_track_ids:
            track_ages[tid] = track_ages.get(tid, 0) + 1
        track_ages = {tid: age for tid, age in track_ages.items() if tid in current_track_ids}

        # --- GLOBAL 1-TO-1 HUNGARIAN ASSIGNMENT ---
        slot_assignments = {} # slot_idx -> det_idx
        if len(detections) > 0:
            costs = np.zeros((len(detections), len(slots)))
            for d_idx in range(len(detections)):
                det_xyxy = detections.xyxy[d_idx]
                det_center = ((det_xyxy[0] + det_xyxy[2]) / 2, (det_xyxy[1] + det_xyxy[3]) / 2)
                
                for s_idx, slot in enumerate(slots):
                    overlap = 0.0
                    if detections.mask is not None:
                        overlap = alignment_engine.calculate_overlap(detections.mask[d_idx], slot.polygon, resolution_wh)
                    
                    slot_center = get_centroid(slot.polygon)
                    dist = np.linalg.norm(np.array(det_center) - np.array(slot_center))
                    
                    # Cost Function: 0.7*Overlap_Inverse + 0.3*Normalized_Dist_Squared
                    costs[d_idx, s_idx] = 0.7 * (1.0 - overlap) + 0.3 * ((dist / max_diag) ** 2)

            row_ind, col_ind = linear_sum_assignment(costs)
            margin = 0.1
            for d_idx, s_idx in zip(row_ind, col_ind):
                min_cost_for_det = np.min(costs[d_idx])
                if costs[d_idx, s_idx] <= min_cost_for_det + margin:
                    slot_assignments[s_idx] = d_idx

        with G_STATE.vision_lock:
            for i, slot in enumerate(slots):
                vehicle_track_id = None
                vehicle_center = None
                vehicle_mask = None
                vehicle_bbox = None
                
                if i in slot_assignments:
                    d_idx = slot_assignments[i]
                    tid = detections.tracker_id[d_idx]
                    box = detections.xyxy[d_idx]
                    vehicle_track_id = tid
                    vehicle_center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
                    vehicle_mask = detections.mask[d_idx] if detections.mask is not None else None
                    vehicle_bbox = (int(box[0]), int(box[1]), int(box[2]-box[0]), int(box[3]-box[1]))

                # --- UNIFIED PRODUCTION LOGIC (No simulation hacks) ---
                if slot.state == SlotState.CHARGING:
                    if vehicle_track_id is not None and slot.locked_track_id != vehicle_track_id:
                        logger.critical(f"[SAFEGUARD] ID Mismatch in Slot {i+1}: Expected {slot.locked_track_id}, got {vehicle_track_id}. Forcing RESET.")
                        slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=vehicle_track_id)
                        with G_STATE.session_lock:
                            if i in G_STATE.sessions:
                                del G_STATE.sessions[i]

                if vehicle_track_id is not None:
                    slot.track_age = track_ages.get(vehicle_track_id, 0)
                    if slot.state == SlotState.FREE or slot.state == SlotState.RESERVED:
                        if slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=vehicle_track_id):
                            with G_STATE.booking_lock:
                                if i in G_STATE.bookings:
                                    logger.info(f"[VISION] Booking FULFILLED: Slot {i+1}")
                                    del G_STATE.bookings[i]
                    elif slot.state in [SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                        if slot.locked_track_id == vehicle_track_id:
                            has_moved = alignment_engine.detect_motion(vehicle_track_id, vehicle_center)
                            if has_moved or (time.time() - slot.last_evaluation_time > 0.3):
                                # ALWAYS use alignment engine
                                score, features = alignment_engine.evaluate_alignment(
                                    vehicle_track_id, vehicle_mask, vehicle_center, 
                                    slot, resolution_wh, slot.track_age, vehicle_bbox
                                )
                                slot.update_alignment(score, features)
                                slot.last_evaluation_time = time.time()
                                
                                if slot.alignment_state == AlignmentState.ALIGNED:
                                    slot.set_state(SlotState.CHARGING, track_id=vehicle_track_id)
                                elif slot.alignment_state == AlignmentState.MISALIGNED:
                                    slot.set_state(SlotState.MISALIGNED, track_id=vehicle_track_id)
                    slot.handle_occlusion(False)
                else:
                    if slot.state != SlotState.FREE and slot.state != SlotState.RESERVED:
                        slot.handle_occlusion(True)

            with G_STATE.queue_lock:
                queue_manager.update(detections, queue_zones)
                queue_manager.manage_reservations(slots)
            
            with G_STATE.booking_lock:
                for i, slot in enumerate(slots):
                    if i in G_STATE.bookings:
                        if slot.state == SlotState.FREE:
                            slot.set_state(SlotState.RESERVED)
                        elif slot.state not in [SlotState.RESERVED, SlotState.ALIGNMENT_PENDING, SlotState.CHARGING, SlotState.MISALIGNED]:
                            pass
                        elif slot.state != SlotState.RESERVED:
                            logger.warning(f"[VISION] Clearing STALE Booking: Slot {i+1} is {slot.state.name}")
                            del G_STATE.bookings[i]

        frame = visualizer.draw_overlays(frame, slots, queue_zones, {})
        frame = visualizer.draw_detections(frame, detections)
        frame = visualizer.draw_sidebar(frame, queue_manager)

        cv2.imshow("Smart EV Charging", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'): break

    if cap: cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
