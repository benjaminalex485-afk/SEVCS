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
from src.queue_manager import QueueManager
from src.slot_state_machine import Slot, SlotState, AlignmentState
from src.alignment_engine import AlignmentEngine
from src.auth_engine import AuthEngine
from src.visualizer import Visualizer
from src import utils

# --- SIMULATION CONFIG ---
USE_FAKE_INPUT = True
ACTIVE_SCENARIO = os.getenv("SEVCS_SCENARIO", "stage2_happy_path")

if USE_FAKE_INPUT:
    from src.fake_detection import ScenarioEngine
    engine = ScenarioEngine(ACTIVE_SCENARIO)
    # Mocking for simulation
    class SlotDetector:
        def __init__(self, *args, **kwargs): pass
        def detect(self, *args, **kwargs): return []
    tracker = None 
else:
    from src.detector import SlotDetector
    import supervision as sv
    tracker = sv.ByteTrack()

# --- CONSTANTS ---
ALIGN_THRESHOLD = 0.75

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
        self.auth_engine = AuthEngine()
        self.sessions = {}          # slot_idx -> {battery_pct, power, energy, start_time}
        self.queue_manager = None   # QueueManager instance
        self.last_vision_heartbeat = time.monotonic()
        self.camera_online = False
        
        self.users_db = [
            {"username": "admin", "password": "admin", "role": "admin"},
            {"username": "user", "password": "user", "role": "user"}
        ]

G_STATE = SystemState()

# --- ATOMIC SNAPSHOT LAYER ---
def get_system_snapshot():
    snapshot = {
        "sys": { "timestamp": time.time() },
        "slots": [],
        "queue": []
    }
    with G_STATE.vision_lock:
        snapshot["sys"]["camera_online"] = G_STATE.camera_online
        snapshot["sys"]["vision_heartbeat"] = G_STATE.last_vision_heartbeat
        for i, slot in enumerate(G_STATE.slots):
            data = {
                "id": i + 1, "state": slot.state.name, "alignment": slot.alignment_state.name,
                "type": "Standard", "state_enter_time": slot.state_enter_time
            }
            with G_STATE.auth_engine.lock:
                data["booking"] = copy.deepcopy(G_STATE.auth_engine.bookings.get(i))
            with G_STATE.session_lock:
                data["session"] = copy.deepcopy(G_STATE.sessions.get(i))
            snapshot["slots"].append(data)
    with G_STATE.queue_lock:
        if G_STATE.queue_manager:
            for i, v in enumerate(G_STATE.queue_manager.queue):
                snapshot["queue"].append({"track_id": v["id"], "position": i + 1, "arrival": v["arrival_time"]})
    snapshot["sys"]["queue_count"] = len(snapshot["queue"])
    snapshot["sys"]["charging_count"] = sum(1 for s in snapshot["slots"] if s["state"] == "CHARGING")
    return snapshot

# --- API SERVER ---
api_app = Flask(__name__, static_folder='ev_charging_sim/data')
CORS(api_app)

@api_app.route('/')
def serve_index(): return send_from_directory(api_app.static_folder, 'index.html')

@api_app.route('/<path:path>')
def serve_static(path): return send_from_directory(api_app.static_folder, path)

@api_app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    u, p = data.get('username'), data.get('password')
    for user in G_STATE.users_db:
        if user["username"] == u and user["password"] == p:
             return jsonify({"status": "success", "role": user["role"]})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401

@api_app.route('/api/status', methods=['GET'])
def get_status(): return jsonify(get_system_snapshot())

@api_app.route('/api/book', methods=['POST'])
def book_slot_api():
    data = request.json or {}
    username = data.get('username', 'Anonymous')
    with G_STATE.vision_lock:
        assigned_idx = -1
        # PRIORITY 1: If vehicle is already present -> bind to that slot
        for i, slot in enumerate(G_STATE.slots):
            if slot.locked_track_id is not None:
                # Still check if it already has an active booking to avoid double-booking same vehicle
                if i not in G_STATE.auth_engine.bookings or G_STATE.auth_engine.is_expired(i):
                    assigned_idx = i
                    logger.info(f"[BOOKING] Binding to physical vehicle at Slot {i+1}")
                    break
        
        # PRIORITY 2: Fallback to first available free slot
        if assigned_idx == -1:
            for i, slot in enumerate(G_STATE.slots):
                is_free = (slot.state == SlotState.FREE)
                no_booking = (i not in G_STATE.auth_engine.bookings or G_STATE.auth_engine.is_expired(i))
                if is_free and no_booking:
                    assigned_idx = i
                    break
        
    if assigned_idx != -1:
        code = G_STATE.auth_engine.generate_booking(assigned_idx, username)
        return jsonify({"status": "success", "slot_id": assigned_idx + 1, "auth_code": code})
    return jsonify({"status": "error", "message": "No slots available"}), 400

@api_app.route('/api/authorize', methods=['POST'])
def authorize_api():
    data = request.json or {}
    slot_id, code = data.get('slot_id'), data.get('code')
    if slot_id is None or code is None: return jsonify({"status": "error", "message": "Missing slot_id or code"}), 400
    idx = slot_id - 1
    with G_STATE.vision_lock:
        if idx < 0 or idx >= len(G_STATE.slots): return jsonify({"status": "error", "code": "wrong_slot"}), 400
        slot = G_STATE.slots[idx]
        if slot.state != SlotState.AUTH_PENDING: return jsonify({"status": "error", "code": "stale_request"}), 400
        if slot.locked_track_id is None: return jsonify({"status": "error", "code": "no_vehicle"}), 400
        current_track_id = slot.locked_track_id
    status, is_idempotent = G_STATE.auth_engine.authorize_vehicle(idx, code, current_track_id)
    if status == "success": return jsonify({"status": "success", "idempotent": is_idempotent})
    return jsonify({"status": "error", "code": status}), 400

# --- CHARGING SIMULATION THREAD ---
def charging_simulation_loop():
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        if now - G_STATE.last_vision_heartbeat > 5.0: continue
        slot_states = []
        with G_STATE.vision_lock:
            for i, slot in enumerate(G_STATE.slots):
                slot_states.append({"idx": i, "state": slot.state, "enter_time": slot.state_enter_time})
        with G_STATE.session_lock:
            for item in slot_states:
                i, state, enter_time = item["idx"], item["state"], item["enter_time"]
                if state == SlotState.CHARGING:
                    if i not in G_STATE.sessions:
                        if now - enter_time > 2.0:
                            G_STATE.sessions[i] = {"battery_pct": 20, "power": 7.2, "energy": 0.0, "start_time": now}
                            logger.info(f"[SESSION] START (CHARGING): Slot {i+1}")
                    else:
                        sess = G_STATE.sessions[i]
                        sess["battery_pct"] = min(100, sess["battery_pct"] + 1)
                        sess["energy"] += (sess["power"] / 3600.0)
                elif state == SlotState.FREE:
                    if i in G_STATE.sessions and now - enter_time > 5.0:
                        del G_STATE.sessions[i]
                        logger.info(f"[SIM] Session CLEANUP: Slot {i+1}")

# --- API SERVER RUNNER ---

def run_api(): api_app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

def get_centroid(polygon):
    M = cv2.moments(polygon)
    if M["m00"] == 0: return np.mean(polygon, axis=0)
    return (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))

def main():
    config = utils.load_config()
    detector = SlotDetector(config.get('model_path'), config.get('class_ids', [2, 3, 5, 7]))
    # tracker is already initialized/mocked at top level
    queue_manager = QueueManager(timeout=10.0)
    alignment_engine = AlignmentEngine()
    visualizer = Visualizer()
    slots = [Slot(i, poly) for i, poly in enumerate(config.get('slots', []))]
    G_STATE.auth_engine.clear_all()
    with G_STATE.vision_lock:
        G_STATE.slots, G_STATE.queue_manager, G_STATE.camera_online = slots, queue_manager, True
    threading.Thread(target=run_api, daemon=True).start()
    threading.Thread(target=charging_simulation_loop, daemon=True).start()
    
    cap = None if USE_FAKE_INPUT else cv2.VideoCapture(config.get('camera_index', 0))
    resolution_wh = (1280, 720)
    max_diag = np.linalg.norm(resolution_wh)
    track_ages = {}
    track_id_loss_timers = {} 
    
    logger.info(f"System Ready. Mode: {'FAKE' if USE_FAKE_INPUT else 'REAL'} | Scenario: {ACTIVE_SCENARIO if USE_FAKE_INPUT else 'N/A'}")
    
    while True:
        now_mono = time.monotonic()
        if USE_FAKE_INPUT:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            detections = engine.get_detections()
            time.sleep(0.001)
            if engine.is_complete(): engine.reset()
        else:
            ret, frame = cap.read()
            if not ret: break
            detections = tracker.update_with_detections(detector.detect(frame, conf=0.15))
        
        with G_STATE.vision_lock:
            G_STATE.last_vision_heartbeat = now_mono
        
        current_track_ids = detections.tracker_id if detections.tracker_id is not None else []
        # Update track ages correctly
        for tid in current_track_ids:
            track_ages[tid] = track_ages.get(tid, 0) + 1
        
        # PRUNE track_ages: Remove IDs not active or locked to a slot
        active_ids = set(current_track_ids)
        for tid in list(track_ages.keys()):
            still_used = (tid in active_ids or any(slot.locked_track_id == tid for slot in slots))
            if not still_used:
                del track_ages[tid]
        
        # --- ASSIGNMENT ---
        slot_assignments = {}
        if len(detections) > 0:
            costs = np.zeros((len(detections), len(slots)))
            MAX_MATCH_COST = 0.6
            for d_idx in range(len(detections)):
                det_xyxy = detections.xyxy[d_idx]
                det_center = ((det_xyxy[0] + det_xyxy[2]) / 2, (det_xyxy[1] + det_xyxy[3]) / 2)
                for s_idx, slot in enumerate(slots):
                    overlap = alignment_engine.calculate_overlap(detections.mask[d_idx], slot.polygon, resolution_wh) if detections.mask is not None else 0.0
                    dist = np.linalg.norm(np.array(det_center) - np.array(slot.centroid))
                    costs[d_idx, s_idx] = 0.7 * (1.0 - overlap) + 0.3 * ((dist / max_diag) ** 2)
            row_ind, col_ind = linear_sum_assignment(costs)
            for d_idx, s_idx in zip(row_ind, col_ind):
                if costs[d_idx, s_idx] <= MAX_MATCH_COST:
                    slot_assignments[s_idx] = d_idx
                else:
                    logger.warning(f"[MATCH] REJECTED Slot {s_idx+1} Det {d_idx} Cost={costs[d_idx, s_idx]:.2f}")

        with G_STATE.vision_lock:
            for i, slot in enumerate(slots):
                vehicle_track_id = None
                vehicle_center = None
                vehicle_mask = None
                vehicle_bbox = None
                is_fallback = False
                
                if i in slot_assignments:
                    d_idx = slot_assignments[i]
                    tid, box = detections.tracker_id[d_idx], detections.xyxy[d_idx]
                    vehicle_track_id, vehicle_mask = tid, (detections.mask[d_idx] if detections.mask is not None else None)
                    vehicle_center = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
                    vehicle_bbox = (int(box[0]), int(box[1]), int(box[2]-box[0]), int(box[3]-box[1]))
                    track_id_loss_timers[i] = 0.0
                else:
                    # 200ms DEBOUNCE
                    if track_id_loss_timers.get(i, 0.0) == 0.0: track_id_loss_timers[i] = now_mono
                    if now_mono - track_id_loss_timers[i] < 0.2:
                         vehicle_track_id, is_fallback = slot.locked_track_id, True
                
                # --- IDENTITY PERSISTENCE ---
                if slot.locked_track_id is not None and vehicle_track_id is not None and vehicle_track_id != slot.locked_track_id:
                    if slot.state not in [SlotState.FREE, SlotState.RESERVED]:
                        logger.warning(f"[AUTH] REVOKE: ID_MISMATCH for Slot {i+1}: Resetting.")
                        G_STATE.auth_engine.revoke_authorization(i)
                        slot.set_state(SlotState.FREE)
                        with G_STATE.session_lock:
                             if i in G_STATE.sessions: del G_STATE.sessions[i]

                # --- ALWAYS UPDATE ALIGNMENT (Except during fallback/occlusion) ---
                if vehicle_track_id is not None and not is_fallback:
                    slot.track_age = track_ages.get(vehicle_track_id, 0)
                    # PER-FRAME EVALUATION (Removed motion gating)
                    score, features = alignment_engine.evaluate_alignment(vehicle_track_id, vehicle_mask, vehicle_center, slot, resolution_wh, slot.track_age, vehicle_bbox)
                    slot.update_alignment(score, features)
                    slot.last_evaluation_time = now_mono

                # --- STABILITY GATED DECISIONS (track_age > 5) ---
                if (slot.track_age > 5 and not is_fallback) or vehicle_track_id is None:
                    # Expiry / Timeout
                    if slot.state == SlotState.AUTH_PENDING:
                        # 60s Timeout for Auth or Expiry
                        has_booking = i in G_STATE.auth_engine.bookings
                        is_expired = G_STATE.auth_engine.is_expired(i)
                        
                        if now_mono - slot.state_enter_time > 60.0:
                            logger.warning(f"[AUTH] REVOKE: TIMEOUT (No action) for Slot {i+1}")
                            G_STATE.auth_engine.revoke_authorization(i)
                            slot.set_state(SlotState.FREE)
                        elif has_booking and is_expired:
                            logger.warning(f"[AUTH] REVOKE: EXPIRED for Slot {i+1}")
                            G_STATE.auth_engine.revoke_authorization(i)
                            slot.set_state(SlotState.FREE)
                        elif slot.alignment_state == AlignmentState.MISALIGNED:
                            logger.warning(f"[ALIGNMENT] DRIFT detected during AUTH_PENDING for Slot {i+1}")
                            slot.set_state(SlotState.ALIGNMENT_PENDING)

                    if vehicle_track_id is not None:
                        if slot.state in [SlotState.FREE, SlotState.RESERVED]:
                            slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=vehicle_track_id)
                        elif slot.state == SlotState.ALIGNMENT_PENDING:
                            if slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD:
                                # Ensure track_id is bound on transition
                                slot.set_state(SlotState.AUTH_PENDING, track_id=vehicle_track_id)
                        elif slot.state == SlotState.AUTH_PENDING:
                            # Explicit Alignment & Auth Guard
                            is_auth = G_STATE.auth_engine.is_authorized(i, vehicle_track_id)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            if is_auth and is_aligned:
                                logger.info(f"[AUTH] PENDING -> ACTIVE for Slot {i+1}")
                                slot.set_state(SlotState.AUTH_ACTIVE, track_id=vehicle_track_id)
                            elif is_auth and not is_aligned:
                                logger.info(f"[ALIGNMENT] BLOCKED: Score={slot.smoothed_alignment_score:.3f} < {ALIGN_THRESHOLD}")

                        elif slot.state == SlotState.AUTH_ACTIVE:
                            is_auth = G_STATE.auth_engine.is_authorized(i, vehicle_track_id)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            
                            if not is_auth:
                                logger.warning(f"[AUTH] REVOKE: EXPIRED during AUTH_ACTIVE for Slot {i+1}")
                                G_STATE.auth_engine.revoke_authorization(i)
                                slot.set_state(SlotState.FREE)
                            elif not is_aligned:
                                logger.warning(f"[AUTH] REVOKE: MISALIGNED during AUTH_ACTIVE for Slot {i+1}")
                                G_STATE.auth_engine.revoke_authorization(i)
                                slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=vehicle_track_id)
                            else:
                                # Both authorized and aligned -> START CHARGING
                                if slot.set_state(SlotState.CHARGING, track_id=vehicle_track_id):
                                    G_STATE.auth_engine.consume_booking(i)
                        
                        elif slot.state == SlotState.CHARGING:
                            is_auth = G_STATE.auth_engine.is_authorized(i, vehicle_track_id)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            if not is_auth or not is_aligned:
                                logger.warning(f"[AUTH] SESSION TERMINATED for Slot {i+1} (Auth={is_auth}, Aligned={is_aligned})")
                                slot.set_state(SlotState.FREE)
                        slot.handle_occlusion(False)
                    else:
                        # 1s Debounce for VEHICLE_LEFT in AUTH_PENDING
                        if slot.state == SlotState.AUTH_PENDING:
                            if not hasattr(slot, 'departure_time'): slot.departure_time = now_mono
                            if now_mono - slot.departure_time > 1.0:
                                logger.warning(f"[AUTH] REVOKE: VEHICLE_LEFT (Confirmed) for Slot {i+1}")
                                G_STATE.auth_engine.revoke_authorization(i)
                                slot.set_state(SlotState.FREE)
                        else:
                            if hasattr(slot, 'departure_time'): del slot.departure_time
                        
                        if slot.state not in [SlotState.FREE, SlotState.RESERVED]:
                            slot.handle_occlusion(True)

            with G_STATE.queue_lock:
                queue_manager.update(detections, config.get('queue_zones', []))
                queue_manager.manage_reservations(slots)

        if not USE_FAKE_INPUT:
            frame = visualizer.draw_overlays(frame, slots, config.get('queue_zones', []), {})
            frame = visualizer.draw_detections(frame, detections)
            frame = visualizer.draw_sidebar(frame, queue_manager)
            cv2.imshow("Smart EV Charging", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
        else:
            # Small wait to prevent 100% CPU but allow high FPS
            time.sleep(0.001)

    if cap: cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__": main()
