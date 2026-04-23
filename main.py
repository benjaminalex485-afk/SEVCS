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
QUEUE_ZONE_POLYGON = [[0, 400], [1280, 400], [1280, 720], [0, 720]]
AUTH_WINDOW = 1.0 # Early authorization window to eliminate API race conditions

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
        self.vision_lock = threading.RLock()
        self.booking_lock = threading.RLock()
        self.session_lock = threading.RLock()
        self.queue_lock = threading.RLock()
        
        # State Data
        self.slots = []             # List of Slot objects
        self.auth_engine = AuthEngine()
        self.sessions = {}          # slot_idx -> {battery_pct, power, energy, start_time}
        self.queue_manager = None   # QueueManager instance
        self.last_vision_heartbeat = utils.now()
        self.camera_online = False
        
        self.users_db = [
            {"username": "admin", "password": "admin", "role": "admin"},
            {"username": "user", "password": "user", "role": "user"}
        ]

G_STATE = SystemState()
CONFIG = utils.load_config()
utils.validate_config(CONFIG)

# --- HARDENING HELPERS ---
def acquire_lock(name, lock):
    """Debug-only lock ordering enforcement."""
    if CONFIG.get("debug_locks", False):
        logger.debug(f"[LOCK] Acquiring {name}")
    lock.acquire()

def release_lock(name, lock):
    if CONFIG.get("debug_locks", False):
        logger.debug(f"[LOCK] Releasing {name}")
    lock.release()

# --- ATOMIC SNAPSHOT LAYER ---
def get_system_snapshot():
    """Atomic multi-subsystem snapshot with strict lock ordering."""
    snapshot = {}
    acquired = []
    
    # 1. Acquire all locks in order
    locks = [
        ("vision", G_STATE.vision_lock),
        ("session", G_STATE.session_lock),
        ("booking", G_STATE.auth_engine.lock),
        ("queue", G_STATE.queue_lock)
    ]
    
    try:
        for name, lock in locks:
            if lock.acquire(timeout=2.0):
                acquired.append(lock)
            else:
                logger.error(f"LOCK TIMEOUT: {name} - Returning partial snapshot")
                break
        
        # 2. Extract Data (Primitive copies only)
        # --- VISION ---
        snapshot["camera_online"] = G_STATE.camera_online
        snapshot["last_heartbeat"] = G_STATE.last_vision_heartbeat
        
        # --- SLOTS ---
        snapshot["slots"] = [s.to_dict() for s in G_STATE.slots]
        
        # --- AUTH/BOOKING ---
        auth_data = G_STATE.auth_engine.to_dict()
        snapshot["bookings"] = auth_data.get("bookings", {})
        
        # --- QUEUE ---
        snapshot["suggestions"] = []
        if G_STATE.queue_manager:
            snapshot["suggestions"] = G_STATE.queue_manager.get_suggestions_snapshot()
            
    except Exception as e:
        logger.error(f"SNAPSHOT ERROR: {e}")
    finally:
        # Release in REVERSE order
        for lock in reversed(acquired):
            lock.release()
        
    return snapshot

# --- API SERVER ---
api_app = Flask(__name__)
CORS(api_app)

@api_app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify(get_system_snapshot())

@api_app.route('/api/suggestions', methods=['GET'])
def get_suggestions():
    snapshot = get_system_snapshot()
    # Extract minimal suggestion schema
    suggestions = []
    for s in snapshot["slots"]:
        suggestions.append({
            "slot_id": s["slot_id"],
            "track_id": s["suggestion"]["track_id"],
            "confidence": s["suggestion"]["confidence"],
            "stable_for": s["suggestion"]["stable_for"],
            "state": s["suggestion"]["state"]
        })
    return jsonify(suggestions)

@api_app.route('/api/queue', methods=['GET'])
def get_queue_api():
    with G_STATE.queue_lock:
        data = []
        if G_STATE.queue_manager:
            for tid, entry in G_STATE.queue_manager.queue.items():
                data.append({
                    "track_id": int(tid),
                    "wait_time": int(time.monotonic() - entry.arrival_time),
                    "priority": round(entry.priority_score, 2),
                    "suggestion": int(entry.assigned_slot) if entry.assigned_slot is not None else None
                })
        return jsonify({"queue": data})

@api_app.route('/api/slots', methods=['GET'])
def get_slots_api():
    with G_STATE.vision_lock:
        data = []
        for slot in G_STATE.slots:
            data.append({
                "slot_id": slot.slot_id + 1,
                "suggested_track_id": slot.suggested_track_id
            })
        return jsonify({"slots": data})

@api_app.route('/api/book', methods=['POST'])
def book_slot_api():
    # ISSUE 4: Rate Limiting
    limit = utils.load_config().get('rate_limit_attempts', 5)
    window = utils.load_config().get('rate_limit_window', 60.0)
    if not G_STATE.auth_engine.check_rate_limit(request.remote_addr, limit, window):
        return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
    G_STATE.auth_engine.record_attempt(request.remote_addr)

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
        timeout = data.get('timeout', 600)
        code = G_STATE.auth_engine.generate_booking(assigned_idx, username, timeout=timeout)
        return jsonify({"status": "success", "slot_id": assigned_idx + 1, "auth_code": code})
    return jsonify({"status": "error", "message": "No slots available"}), 400

@api_app.route('/api/authorize', methods=['POST'])
def authorize_api():
    # ISSUE 4: Rate Limiting
    limit = utils.load_config().get('rate_limit_attempts', 5)
    window = utils.load_config().get('rate_limit_window', 60.0)
    if not G_STATE.auth_engine.check_rate_limit(request.remote_addr, limit, window):
        return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
    G_STATE.auth_engine.record_attempt(request.remote_addr)

    data = request.json or {}
    slot_id, code = data.get('slot_id'), data.get('code')
    if slot_id is None or code is None: return jsonify({"status": "error", "message": "Missing slot_id or code"}), 400
    idx = slot_id - 1
    now_mono = time.monotonic()
    
    with G_STATE.vision_lock:
        if idx < 0 or idx >= len(G_STATE.slots): return jsonify({"status": "error", "code": "wrong_slot"}), 400
        slot = G_STATE.slots[idx]
        
        # STABILIZED STAGE 2: Early Authorization Window
        is_pending = (slot.state == SlotState.AUTH_PENDING)
        is_early_window = (slot.state == SlotState.ALIGNMENT_PENDING and (now_mono - slot.state_enter_time <= AUTH_WINDOW))
        
        if not (is_pending or is_early_window):
            return jsonify({"status": "error", "code": "stale_request"}), 400
            
        if slot.locked_track_id is None:
            return jsonify({"status": "error", "code": "no_vehicle"}), 400
            
        # STAGE 3.5: Occlusion Debounce Safety
        if slot.is_in_occlusion_debounce():
            logger.warning(f"[AUTH] REJECTED Early Auth for Slot {idx+1}: UNSTABLE_TRACK (Occluded)")
            return jsonify({"status": "error", "code": "unstable_track"}), 400
            
        current_track_id = slot.locked_track_id
        
    status, is_idempotent = G_STATE.auth_engine.authorize_vehicle(idx, code, current_track_id)
    if status == "success": return jsonify({"status": "success", "idempotent": is_idempotent})
    return jsonify({"status": "error", "code": status}), 400

def run_api():
    api_app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)

def charging_simulation_loop():
    while True:
        with G_STATE.session_lock:
            for slot_idx in list(G_STATE.sessions.keys()):
                session = G_STATE.sessions[slot_idx]
                if session['battery_pct'] < 100:
                    session['battery_pct'] += 0.1
                    session['energy'] += 0.05
                else:
                    logger.info(f"Slot {slot_idx+1} Fully Charged.")
        time.sleep(1.0)

# --- MAIN LOOP ---
def main():
    config = utils.load_config()
    detector = SlotDetector(config.get('model_path'), config.get('class_ids', [2, 3, 5, 7]))
    # tracker is already initialized/mocked at top level
    queue_manager = QueueManager()
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
    
    logger.info(f"System Ready. Mode: {'FAKE' if USE_FAKE_INPUT else 'REAL'} | Scenario: {ACTIVE_SCENARIO if USE_FAKE_INPUT else 'N/A'}")
    
    last_heartbeat = 0
    while True:
        now_mono = utils.now()
        if USE_FAKE_INPUT:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            detections = engine.get_detections()
            time.sleep(0.001)
            if engine.is_complete(): engine.reset()
        else:
            ret, frame = cap.read()
            if not ret: break
            detections = tracker.update_with_detections(detector.detect(frame, conf=0.15))
        
        # --- ATOMIC FRAME PROCESSING ---
        with G_STATE.vision_lock:
            G_STATE.last_vision_heartbeat = now_mono
            current_track_ids = set(detections.tracker_id) if detections.tracker_id is not None else set()
            for tid in current_track_ids:
                track_ages[tid] = track_ages.get(tid, 0) + 1
            
            # --- DEFENSIVE GUARD: Clear Stale Assignments ---
            for slot in slots:
                if slot.assigned_track_id is not None and slot.assigned_track_id not in current_track_ids:
                    slot.assigned_track_id = None

            # --- QUEUE MANAGEMENT (STAGE 3) ---
            with G_STATE.queue_lock:
                if G_STATE.queue_manager:
                    # Correct nesting: CONFIG['queue_zone']['polygon'] is a list of points [[x,y],...]
                    q_zone = CONFIG.get("queue_zone", {}).get("polygon", QUEUE_ZONE_POLYGON)
                    G_STATE.queue_manager.update_queue(detections, [q_zone])
                    G_STATE.queue_manager.update_suggestions(slots)
            
            # --- ASSIGNMENT ---
            matched_slots = set()
            if len(detections) > 0:
                costs = np.zeros((len(detections), len(slots)))
                overlaps = np.zeros((len(detections), len(slots)))
                centroid_scores = np.zeros((len(detections), len(slots)))
                MAX_MATCH_COST = 0.6
                
                for d_idx in range(len(detections)):
                    det_xyxy = detections.xyxy[d_idx]
                    det_center = ((det_xyxy[0] + det_xyxy[2]) / 2, (det_xyxy[1] + det_xyxy[3]) / 2)
                    for s_idx, slot in enumerate(slots):
                        overlap = alignment_engine.calculate_overlap(detections.mask[d_idx], slot.polygon, resolution_wh) if detections.mask is not None else 0.0
                        overlaps[d_idx, s_idx] = overlap
                        dist = np.linalg.norm(np.array(det_center) - np.array(slot.centroid))
                        centroid_score = 1.0 - (dist / max_diag)
                        centroid_scores[d_idx, s_idx] = centroid_score
                        costs[d_idx, s_idx] = 0.7 * (1.0 - overlap) + 0.3 * (1.0 - centroid_score)
                
                row_ind, col_ind = linear_sum_assignment(costs)
                assigned_this_frame = {} # tid -> slot_idx
                
                for d_idx, s_idx in zip(row_ind, col_ind):
                    if costs[d_idx, s_idx] <= MAX_MATCH_COST:
                        slot = slots[s_idx]
                        tid = detections.tracker_id[d_idx]
                        
                        # INVARIANT: Assignment Uniqueness
                        if tid in assigned_this_frame:
                            if CONFIG.get("strict_mode", False):
                                raise RuntimeError("Duplicate assignment detected")
                            else:
                                slot.force_safe_state()
                                slots[assigned_this_frame[tid]].force_safe_state()
                                continue
                        
                        assigned_this_frame[tid] = s_idx
                        matched_slots.add(s_idx)
                        
                        overlap = overlaps[d_idx, s_idx]
                        c_score = centroid_scores[d_idx, s_idx]
                        final_score = 0.7 * overlap + 0.3 * c_score
                        slot.update_alignment(final_score, {"overlap_ratio": overlap, "centroid_score": c_score})
                        
                        # --- STATE MACHINE ---
                        if slot.state == SlotState.AUTH_PENDING:
                            is_auth, reason = G_STATE.auth_engine.is_authorized(s_idx, tid)
                            has_booking = (s_idx in G_STATE.auth_engine.bookings)
                            is_expired = G_STATE.auth_engine.is_expired(s_idx)
                            
                            timeout = CONFIG.get('auth_timeout', 60.0)
                            if now_mono - slot.state_enter_time > timeout:
                                logger.warning(f"[AUTH] REVOKE: TIMEOUT for Slot {s_idx+1}")
                                G_STATE.auth_engine.revoke_authorization(s_idx)
                                slot.set_state(SlotState.FREE)
                            elif has_booking and is_expired:
                                logger.warning(f"[AUTH] REVOKE: EXPIRED for Slot {s_idx+1}")
                                G_STATE.auth_engine.revoke_authorization(s_idx)
                                slot.set_state(SlotState.FREE)
                            elif reason == "ID_MISMATCH":
                                logger.warning(f"[AUTH] REVOKE: ID_MISMATCH for Slot {s_idx+1}")
                                G_STATE.auth_engine.revoke_authorization(s_idx)
                                slot.set_state(SlotState.FREE)
                            elif is_auth:
                                is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                                if is_aligned:
                                    logger.info(f"[AUTH] PENDING -> ACTIVE for Slot {s_idx+1}")
                                    slot.set_state(SlotState.AUTH_ACTIVE, track_id=tid)

                        elif slot.state in [SlotState.FREE, SlotState.RESERVED]:
                            slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=tid)
                        elif slot.state == SlotState.ALIGNMENT_PENDING:
                            if slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD:
                                slot.set_state(SlotState.AUTH_PENDING, track_id=tid)
                        
                        elif slot.state == SlotState.AUTH_ACTIVE:
                            is_auth, reason = G_STATE.auth_engine.is_authorized(s_idx, tid)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            if not is_auth:
                                logger.warning(f"[AUTH] REVOKE: {reason} during AUTH_ACTIVE for Slot {s_idx+1}")
                                G_STATE.auth_engine.revoke_authorization(s_idx)
                                slot.set_state(SlotState.FREE)
                            elif not is_aligned:
                                logger.warning(f"[AUTH] REVOKE: MISALIGNED during AUTH_ACTIVE for Slot {s_idx+1}")
                                G_STATE.auth_engine.revoke_authorization(s_idx)
                                slot.set_state(SlotState.ALIGNMENT_PENDING, track_id=tid)
                            else:
                                if slot.set_state(SlotState.CHARGING, track_id=tid):
                                    G_STATE.auth_engine.consume_booking(s_idx)
                                    logger.info(f"[AUTH] AUTH_ACTIVE -> CHARGING for Slot {s_idx+1}")
                                    logger.info(f"VALIDATED Session Start for Slot {s_idx+1}")

                        elif slot.state == SlotState.CHARGING:
                            is_auth, _ = G_STATE.auth_engine.is_authorized(s_idx, tid)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            if not is_auth or not is_aligned:
                                logger.warning(f"[AUTH] SESSION TERMINATED for Slot {s_idx+1}")
                                slot.set_state(SlotState.FREE)
                        slot.handle_occlusion(False)

            # --- DEPARTURE & GHOST RECOVERY ---
            for i, slot in enumerate(slots):
                # Ghost Charging check
                if slot.state == SlotState.CHARGING and slot.locked_track_id is None:
                    logger.critical(f"[INVARIANT] Ghost Charging on Slot {i+1} -> FORCED RESET")
                    slot.force_safe_state()
                    G_STATE.auth_engine.revoke_authorization(i)

                if i not in matched_slots:
                    if slot.state == SlotState.AUTH_PENDING:
                        if not hasattr(slot, 'departure_time'): slot.departure_time = now_mono
                        if now_mono - slot.departure_time > 1.0:
                            logger.warning(f"[AUTH] REVOKE: VEHICLE_LEFT for Slot {i+1}")
                            G_STATE.auth_engine.revoke_authorization(i)
                            slot.set_state(SlotState.FREE)
                    else:
                        if hasattr(slot, 'departure_time'): del slot.departure_time
                    if slot.state not in [SlotState.FREE, SlotState.RESERVED]:
                        slot.handle_occlusion(True)

        if not USE_FAKE_INPUT:
            frame = visualizer.draw_overlays(frame, slots, config.get('queue_zones', []), {})
            frame = visualizer.draw_detections(frame, detections)
            frame = visualizer.draw_sidebar(frame, queue_manager)
            cv2.imshow("Smart EV Charging", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
        else:
            time.sleep(0.033)

    if cap: cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__": main()
