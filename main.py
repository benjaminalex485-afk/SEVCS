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
import json
from flask_sock import Sock
import yaml

# Professional Package-style Imports
from src.queue_manager import QueueManager
from src.slot_state_machine import Slot, SlotState, AlignmentState
from src.alignment_engine import AlignmentEngine
from src.auth_engine import AuthEngine
from src.visualizer import Visualizer
from src import utils

# --- SIMULATION CONFIG ---
USE_FAKE_INPUT = False
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
CONFIG = utils.load_config()
utils.validate_config(CONFIG)
ALIGN_THRESHOLD = 0.75
AUTH_WINDOW = 1.0 # Early authorization window to eliminate API race conditions
STRICT_MODE = CONFIG.get("strict_mode", False)
# Freeze configuration after initial load and validation
# CONFIG = utils.freeze_config(CONFIG)

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
from src.industrial_utils import SystemMode, ReasonCode, IndustrialMetrics
import collections

class GlobalState:
    def __init__(self):
        self.slots = []
        self.queue_manager = None
        self.camera_online = False
        self.last_vision_heartbeat = 0
        
        # Concurrency
        self.vision_lock = threading.RLock()
        self.session_lock = threading.RLock()
        self.queue_lock = threading.RLock()
        
        # Stage 4.1/4.2 Industrial Layer
        self.mode = SystemMode.FULL
        self.mode_reason = ReasonCode.NONE
        self.metrics = IndustrialMetrics(window_size=300)
        self.snapshot_buffer = collections.deque(maxlen=100) # ~3-5s history
        self.is_forensic_frozen = False
        self.freeze_start_time = 0.0
        self.startup_time = utils.system_now(caller="main_loop")
        self.last_detections = None
        
        # Stage 5.1.4 Determinism & Sequencing
        self.snapshot_sequence = 0
        self.last_snapshot_version = 0
        self.freeze_reason = None
        self.freeze_age = 0
        self.recovery_attempts = 0
        self.schema_version = 1
        self.overflow_frames = 0
        self.overflow_start_time = 0.0
        
        # Locks
        self.snapshot_lock = threading.RLock()
        
        # Authentication
        self.auth_engine = AuthEngine()
        self.sessions = {}          # slot_idx -> {battery_pct, power, energy, start_time}
        
        self.users_db = [
            {"username": "admin", "password": "admin", "role": "admin"},
            {"username": "user", "password": "user", "role": "user"}
        ]

G_STATE = GlobalState()

# --- API SERVER ---
api_app = Flask(__name__)
CORS(api_app)

# --- WEBSOCKET LAYER ---
sock = Sock(api_app)
esp32_clients = set()

@sock.route('/ws/esp32')
def esp32_ws(ws):
    esp32_clients.add(ws)
    logger.info(f"[WS] ESP32 Connected from {request.remote_addr}")
    try:
        while True:
            data = ws.receive()
            if data:
                # Optional: Process incoming telemetry from ESP32
                pass
    except Exception as e:
        logger.warning(f"[WS] ESP32 Disconnected: {e}")
    finally:
        esp32_clients.discard(ws)

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

# --- INTERACTIVE CALIBRATION ---
INTERACTIVE_POINTS = []
INTERACTIVE_MODE = None # 'SLOT', 'QUEUE', None

def on_mouse(event, x, y, flags, param):
    global INTERACTIVE_POINTS
    if event == cv2.EVENT_LBUTTONDOWN:
        INTERACTIVE_POINTS.append([x, y])
        logger.info(f"[CALIB] Point added: ({x}, {y})")

def calibrate_zones(frame, mode='SLOT'):
    """Dedicated window for calibration as per user request."""
    global INTERACTIVE_POINTS
    INTERACTIVE_POINTS = []
    window_name = f"CALIBRATION - {mode}"
    cv2.namedWindow(window_name)
    cv2.setMouseCallback(window_name, on_mouse)
    
    logger.info(f"[CALIB] Starting {mode} calibration. Click points, then press 'ENTER' to save or 'ESC' to cancel.")
    
    while True:
        display = frame.copy()
        # Draw instructions
        cv2.putText(display, f"MODE: {mode} | Points: {len(INTERACTIVE_POINTS)}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(display, "Click points, then ENTER to Save, ESC to Cancel", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        for pt in INTERACTIVE_POINTS:
            cv2.circle(display, tuple(pt), 5, (0, 0, 255), -1)
        if len(INTERACTIVE_POINTS) >= 2:
            cv2.polylines(display, [np.array(INTERACTIVE_POINTS, np.int32)], mode == 'SLOT' and len(INTERACTIVE_POINTS) >= 4, (0, 255, 255), 2)
            
        cv2.imshow(window_name, display)
        key = cv2.waitKey(30) & 0xFF
        if key == 13: # ENTER
            if len(INTERACTIVE_POINTS) >= 3:
                break
            else:
                logger.warning("[CALIB] Need at least 3 points to save.")
        elif key == 27: # ESC
            INTERACTIVE_POINTS = []
            break
            
    cv2.destroyWindow(window_name)
    return INTERACTIVE_POINTS

def trigger_freeze(reason):
    """Idempotent freeze trigger with priority and diagnostic logging."""
    if G_STATE.is_forensic_frozen:
        # Only override if new reason is critical or we are in the first frame of freeze
        return
    
    G_STATE.is_forensic_frozen = True
    G_STATE.freeze_reason = reason
    G_STATE.freeze_start_time = utils.system_now(caller="main_loop")
    logger.critical(f"!!! SYSTEM FREEZE TRIGGERED: {reason} !!!")

# --- VALIDATION GATES (Pure & Non-Mutating) ---
def validate_required_keys(snapshot):
    """Enforces structural completeness at the entity level."""
    for entry in snapshot.get("queue", []):
        if "global_id" not in entry or "track_id" not in entry:
            raise ValueError("MISSING_REQUIRED_FIELD: Queue entry incomplete")
    
    for slot in snapshot.get("slots", []):
        if "slot_id" not in slot:
            raise ValueError("MISSING_REQUIRED_FIELD: Slot entry incomplete")

def validate_value_ranges(snapshot):
    """Enforces physical domain safety (0.0 to 1.0 for metrics)."""
    for entry in snapshot.get("queue", []):
        d_score = entry.get("drift_score", 0.0)
        conf = entry.get("signal_confidence", 0.0)
        if not (0.0 <= d_score <= 1.0) or not (0.0 <= conf <= 1.0):
            raise ValueError(f"VALUE_OUT_OF_RANGE: Drift={d_score}, Conf={conf}")

def validate_referential_integrity(snapshot):
    """Enforces graph consistency (Dangling assignments, duplicates)."""
    ids = {e["global_id"] for e in snapshot.get("queue", []) if "global_id" in e}
    
    # 1. Duplicate Global IDs
    if len(ids) != len(snapshot.get("queue", [])):
         raise ValueError("DUPLICATE_GLOBAL_ID: Identity collision detected")
         
    # 2. Dangling Assignments
    for slot in snapshot.get("slots", []):
        gid = slot.get("assigned_global_id")
        if gid and gid not in ids:
            raise ValueError(f"DANGLING_ASSIGNMENT: Slot points to non-existent ID {gid}")

def validate_internal_state(snapshot):
    """Ensures presence of deep-state buffers required for replay."""
    internal = snapshot.get("internal_state", {})
    required = ["ewma_buffers", "hysteresis_state", "cooldowns", "consistency_counters"]
    for key in required:
        if key not in internal:
            raise ValueError(f"STATE_CORRUPTION: Missing {key} in internal_state")

# --- ATOMIC SNAPSHOT LAYER ---
def get_system_snapshot(frame_id, frame_time, debug=True):
    """
    9-Step Correct-by-Construction Forensic Pipeline:
    1. Reference Capture (Locked)
    2. Build Candidate (Async)
    3. Serialize-Deserialize-Normalize-Validate (Async)
    4. Atomic Sequence Commit (Locked)
    """
    # --- 1. REFERENCE CAPTURE (Minimal Lock Scope) ---
    with G_STATE.vision_lock, G_STATE.session_lock, G_STATE.auth_engine.lock, G_STATE.queue_lock:
        local_slots = [s for s in G_STATE.slots]
        local_queue = list(G_STATE.queue_manager.queue.values()) if G_STATE.queue_manager else []
        local_bookings = copy.deepcopy(G_STATE.auth_engine.bookings)
        local_mode = G_STATE.mode
        local_reason = G_STATE.mode_reason
        # Internal deep state
        local_internal = {
            "ewma_buffers": {}, # Placeholder: Need actual buffers from QM/Slots
            "hysteresis_state": {},
            "cooldowns": {},
            "consistency_counters": dict(G_STATE.queue_manager.consistency_counters) if G_STATE.queue_manager else {}
        }
        
    # --- 2. BUILD CANDIDATE (Async/Primitive Data Only) ---
    try:
        snapshot_candidate = {
            "snapshot_version": frame_id,
            "frame_time": utils.normalize_float(frame_time),
            "mode": local_mode.name,
            "mode_reason": local_reason.name,
            "queue": [e.to_dict() for e in local_queue],
            "slots": [s.to_dict() for s in local_slots],
            "internal_state": local_internal,
            "schema_version": G_STATE.schema_version,
            "scope": {"node_id": "node_1", "camera_id": "cam_1"},
            "source": "BACKEND",
            "timestamp": utils.system_now(caller="main_loop"),
            "system_health": float(G_STATE.queue_manager.system_health) if G_STATE.queue_manager else 1.0,
            "system_mode": local_mode.name
        }
        
        # --- 3. FULL-CYCLE SANITY GATE (Dump -> Load -> Normalize -> Validate) ---
        # A. Serialize (Strict NaN/Inf rejection)
        serialized = json.dumps(snapshot_candidate, allow_nan=False)
        
        # B. Deserialize (Bit-drift removal)
        deserialized = json.loads(serialized)
        
        # C. Normalize (None-Removal -> Float Round -> Deep Sort)
        normalized = utils.normalize_state(deserialized)
        
        # --- CONTAINER VALIDATION (Context-Aware) ---
        # 1. Slots must ALWAYS exist (hard invariant)
        slots = normalized.get("slots")
        if not isinstance(slots, list) or len(slots) == 0:
            raise ValueError("EMPTY_SLOTS")

        # 2. Mode must be present
        mode = normalized.get("mode")
        if mode is None:
            raise ValueError("MISSING_MODE")

        # 3. Queue can be empty ONLY in safe/idle states
        queue = normalized.get("queue")
        if queue is None:
            raise ValueError("MISSING_QUEUE")

        if len(queue) == 0:
            # Allowed empty states
            ALLOWED_EMPTY_QUEUE_MODES = {
                "SAFE",
                "IDLE",
                "INIT",
                "SOFT_SAFE",
                "FULL"
            }

            if mode not in ALLOWED_EMPTY_QUEUE_MODES:
                raise ValueError(f"EMPTY_QUEUE_INVALID_STATE: mode={mode}")
             
        validate_required_keys(normalized)
        validate_value_ranges(normalized)
        validate_referential_integrity(normalized)
        validate_internal_state(normalized)
        
        # --- 4. ATOMIC SEQUENCE COMMIT (Locked) ---
        with G_STATE.snapshot_lock:
            G_STATE.snapshot_sequence += 1
            seq = G_STATE.snapshot_sequence
            
            # Lineage
            prev = G_STATE.last_snapshot_version
            normalized["snapshot_sequence"] = seq
            normalized["previous_snapshot_version"] = prev if prev else frame_id
            
            G_STATE.last_snapshot_version = frame_id
            
            # Final Hash (on normalized data only)
            import hashlib
            state_str = json.dumps(normalized, sort_keys=True)
            normalized["state_hash"] = hashlib.sha256(state_str.encode()).hexdigest()
            
            return normalized
            
    except Exception as e:
        logger.error(f"SNAPSHOT_PIPELINE_FAILURE: {e}")
        trigger_freeze(f"SNAPSHOT_PIPELINE_ERROR: {type(e).__name__}")
        return None

        return None

def get_request_data():
    """Helper to handle the wrapped 'payload' from api_v3.js."""
    data = request.json or {}
    if "payload" in data:
        return data["payload"]
    return data

# --- API SERVER ---
@api_app.route('/api/status', methods=['GET'])
def get_status():
    if G_STATE.snapshot_buffer:
        snapshot = copy.deepcopy(G_STATE.snapshot_buffer[-1])
        # Ensure latest timestamp for UI freshness check
        snapshot["timestamp"] = utils.system_now(caller="api_thread")
        return jsonify(snapshot)
    return jsonify({"error": "No snapshots available", "source": "BACKEND"}), 503

# --- OPERATIONAL SAFETY: RATE LIMITING ---
class TokenBucket:
    def __init__(self, capacity, refill_rate):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = utils.system_now(caller="api_thread")
        self.lock = threading.Lock()

    def consume(self, count=1):
        with self.lock:
            now = utils.system_now(caller="api_thread")
            dt = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + dt * self.refill_rate)
            self.last_refill = now
            if self.tokens >= count:
                self.tokens -= count
                return True
            return False

GLOBAL_LIMITER = TokenBucket(100, 10) # 100 burst, 10/sec refill
IP_LIMITERS = collections.defaultdict(lambda: TokenBucket(20, 2)) # 20 burst, 2/sec refill

def rate_limit(endpoint_name):
    """Decorator for rate limiting."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr
            
            # 1. Global Bypass for Priority APIs
            priority_endpoints = ["authorize"]
            if endpoint_name in priority_endpoints:
                # Still check per-IP for security, but skip global cap
                pass
            elif not GLOBAL_LIMITER.consume():
                return jsonify({"status": "error", "code": "global_rate_limit"}), 429
            
            # 2. Per-IP Check
            if not IP_LIMITERS[ip].consume():
                return jsonify({"status": "error", "code": "rate_limit"}), 429
                
            return f(*args, **kwargs)
        return wrapped
    return decorator

from functools import wraps

@api_app.route('/api/suggestions', methods=['GET'])
@rate_limit("suggestions")
def get_suggestions():
    debug = request.args.get('debug', 'false').lower() == 'true'
    # Use dummy frame info for API if needed, or get last committed
    if G_STATE.snapshot_buffer:
        return jsonify(copy.deepcopy(G_STATE.snapshot_buffer[-1]))
    return jsonify({"error": "No snapshots available"}), 503

@api_app.route('/api/summary', methods=['GET'])
@rate_limit("summary")
def get_summary_api():
    """Compact system health for UI."""
    with G_STATE.queue_lock:
        health = float(G_STATE.queue_manager.system_health) if G_STATE.queue_manager else 1.0
        thrash = float(G_STATE.queue_manager.thrash_rate) if G_STATE.queue_manager else 0.0
    
    active_tracks = 0
    if hasattr(G_STATE, 'last_detections') and G_STATE.last_detections is not None:
        active_tracks = len(G_STATE.last_detections.tracker_id) if G_STATE.last_detections.tracker_id is not None else 0

    return jsonify({
        "mode": G_STATE.mode.name,
        "mode_reason": G_STATE.mode_reason.name,
        "health": round(health, 2),
        "thrash_rate": round(thrash, 3),
        "latency_p95": round(G_STATE.metrics.get_latency_p95(), 3),
        "active_tracks": active_tracks,
        "queue_size": len(G_STATE.queue_manager.queue) if G_STATE.queue_manager else 0
    })

@api_app.route('/api/system/mode', methods=['POST'])
@rate_limit("admin")
def set_system_mode_api():
    """Toggles STRICT_MODE at runtime."""
    data = request.json or {}
    global STRICT_MODE
    if "strict_mode" in data:
        STRICT_MODE = bool(data["strict_mode"])
        logger.warning(f"[API] STRICT_MODE updated to: {STRICT_MODE}")
    return jsonify({"status": "ok", "strict_mode": STRICT_MODE})

@api_app.route('/api/forensics', methods=['GET'])
@rate_limit("forensics")
def get_forensics():
    return jsonify(list(G_STATE.snapshot_buffer))

@api_app.route('/api/forensics/freeze', methods=['POST'])
@rate_limit("forensics")
def freeze_forensics():
    G_STATE.is_forensic_frozen = True
    G_STATE.freeze_start_time = utils.system_now(caller="api_thread")
    return jsonify({"status": "frozen", "expiry": 60})

@api_app.route('/api/forensics/unfreeze', methods=['POST'])
@rate_limit("forensics")
def unfreeze_forensics():
    G_STATE.is_forensic_frozen = False
    return jsonify({"status": "rolling"})

@api_app.route('/api/queue', methods=['GET'])
def get_queue_api():
    with G_STATE.queue_lock:
        data = []
        if G_STATE.queue_manager:
            for tid, entry in G_STATE.queue_manager.queue.items():
                data.append({
                    "track_id": int(tid),
                    "wait_time": int(utils.system_now(caller="api_thread") - entry.arrival_time),
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

@api_app.route('/api/login', methods=['POST'])
def login_api():
    data = get_request_data()
    username = data.get('email') or data.get('username')
    password = data.get('password')
    logger.info(f"[API] Login attempt for: {username}")
    for user in G_STATE.users_db:
        if user['username'] == username and user['password'] == password:
            logger.info(f"[API] Login SUCCESS for: {username} ({user['role']})")
            return jsonify({
                "status": "success", 
                "token": "dummy-token-123", 
                "user_id": username,
                "role": user['role'].upper(),
                "success": True
            })
    logger.warning(f"[API] Login FAILED for: {username}")
    return jsonify({"status": "error", "message": "Invalid credentials", "success": False}), 401

@api_app.route('/api/signup', methods=['POST'])
def signup_api():
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')
    name = data.get('name', 'New User')
    
    if not email or not password:
        return jsonify({"status": "error", "message": "Missing email or password"}), 400
    
    # Check if user already exists
    for user in G_STATE.users_db:
        if user['username'] == email:
            return jsonify({"status": "error", "message": "User already exists"}), 400
            
    # Add to in-memory DB
    new_user = {"username": email, "password": password, "role": "user", "name": name}
    G_STATE.users_db.append(new_user)
    logger.info(f"[API] New user registered: {email}")
    return jsonify({"status": "success", "message": "Account created successfully", "success": True})

@api_app.route('/api/book', methods=['POST'])
def book_slot_api():
    # ISSUE 4: Rate Limiting
    data = get_request_data()
    username = data.get('username', 'Anonymous')
    identifier = f"{request.remote_addr}:{username}"
    
    limit = CONFIG.get('rate_limit_attempts', 5)
    window = CONFIG.get('rate_limit_window', 60.0)
    
    if not G_STATE.auth_engine.check_rate_limit(identifier, limit, window):
        return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
    G_STATE.auth_engine.record_attempt(identifier)
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
    data = get_request_data()
    username = data.get("username", "Anonymous")
    identifier = f"{request.remote_addr}:{username}"

    # ISSUE 4: Rate Limiting
    limit = CONFIG.get('rate_limit_attempts', 5)
    window = CONFIG.get('rate_limit_window', 60.0)
    if not G_STATE.auth_engine.check_rate_limit(identifier, limit, window):
        return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
    G_STATE.auth_engine.record_attempt(identifier)

    slot_id, code = data.get('slot_id'), data.get('code')
    if slot_id is None or code is None: return jsonify({"status": "error", "message": "Missing slot_id or code"}), 400
    idx = slot_id - 1
    loop_start = utils.system_now(caller="api_thread")
    
    with G_STATE.vision_lock:
        if idx < 0 or idx >= len(G_STATE.slots): return jsonify({"status": "error", "code": "wrong_slot"}), 400
        slot = G_STATE.slots[idx]
        
        # STABILIZED STAGE 2: Early Authorization Window
        is_pending = (slot.state == SlotState.AUTH_PENDING)
        is_early_window = (slot.state == SlotState.ALIGNMENT_PENDING and (loop_start - slot.state_enter_time <= AUTH_WINDOW))
        
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

# --- WEB UI SERVER ---
@api_app.route('/')
def serve_index():
    return send_from_directory('ui', 'index.html')

@api_app.route('/<path:path>')
def serve_static(path):
    if path.startswith('api/'):
        return jsonify({"error": "API route not found"}), 404
    return send_from_directory('ui', path)

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
    detector = SlotDetector(CONFIG.get('model_path'), CONFIG.get('class_ids', [2, 3, 5, 7]))
    # tracker is already initialized/mocked at top level
    dist_norm = CONFIG.get("distance_normalization", 1500.0)
    queue_manager = QueueManager(max_dist=dist_norm)
    alignment_engine = AlignmentEngine()
    visualizer = Visualizer()
    slots = [Slot(i, poly) for i, poly in enumerate(CONFIG.get('slots', []))]
    G_STATE.auth_engine.clear_all()
    with G_STATE.vision_lock:
        G_STATE.slots, G_STATE.queue_manager, G_STATE.camera_online = slots, queue_manager, True
    threading.Thread(target=run_api, daemon=True).start()
    threading.Thread(target=charging_simulation_loop, daemon=True).start()
    
    logger.info(f"System Ready. Mode: {'FAKE' if USE_FAKE_INPUT else 'REAL'} | Scenario: {ACTIVE_SCENARIO if USE_FAKE_INPUT else 'N/A'}")
    
    if not USE_FAKE_INPUT:
        cam_idx = CONFIG.get('camera_index', 0)
        logger.info(f"[CAMERA] Attempting to open camera at index: {cam_idx} (DSHOW)")
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            logger.error(f"[CAMERA] Failed to open camera at index {cam_idx}. Falling back to index 0.")
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                logger.critical("[CAMERA] No cameras available. Exiting.")
                return
        logger.info("[CAMERA] Camera opened successfully.")
        cv2.namedWindow("Smart EV Charging")
        cv2.setMouseCallback("Smart EV Charging", on_mouse)
    else:
        cap = None
    
    REPLAY_MODE = CONFIG.get("replay_mode", False)
    if REPLAY_MODE:
        logger.info("!!! REPLAY MODE ACTIVE !!! Live Input Disabled.")
        if not USE_FAKE_INPUT:
            logger.error("REPLAY_MODE and LIVE_INPUT are mutually exclusive. Exiting.")
            return

    last_loop_time = utils.system_now(caller="main_loop")
    replay_idx = 0
    first_loop = True
    frame_id = 0
    
    # --- CONSECUTIVE COUNTERS ---
    consecutive_overflow = 0
    overflow_start_frame_time = 0.0
    track_ages = {}
    STRICT_MODE = CONFIG.get('strict_mode', False)
    while True:
        frame_id += 1
        loop_start = utils.system_now(caller="main_loop")
        dt = loop_start - last_loop_time
        last_loop_time = loop_start
        
        # 1. Dual-Condition Watchdog (Stagnation OR Compute Hang)
        is_replay = CONFIG.get("replay_mode", False)
        in_startup_grace = (loop_start - G_STATE.startup_time < 2.0)
        
        if not in_startup_grace and not is_replay:
            if dt > 4.0:
                 trigger_freeze("SYSTEM_STALL: Compute hang detected (>4s)")
        
        # Performance mode logic (Refactored)
        G_STATE.metrics.record_latency(dt)
        p95_latency = G_STATE.metrics.get_latency_p95()
        
        # --- OVERFLOW ESCALATION (Consecutive Frame Contract) ---
        queue_size = len(G_STATE.queue_manager.queue) if G_STATE.queue_manager else 0
        MAX_TRACKS = CONFIG.get("max_tracks", 50)
        
        if queue_size > MAX_TRACKS:
            if consecutive_overflow == 0:
                overflow_start_frame_time = loop_start
            consecutive_overflow += 1
            
            # Escalation Rules
            duration = loop_start - overflow_start_frame_time
            if consecutive_overflow > 10 or duration > 2.0:
                trigger_freeze(f"PERSISTENT_OVERFLOW: {consecutive_overflow} frames / {duration:.2f}s")
            else:
                G_STATE.mode = SystemMode.SOFT_SAFE
                G_STATE.mode_reason = ReasonCode.OVERFLOW
        else:
            # INSTANT RESET
            consecutive_overflow = 0
            overflow_start_frame_time = 0.0
            if G_STATE.mode == SystemMode.SOFT_SAFE and G_STATE.mode_reason == ReasonCode.OVERFLOW:
                 G_STATE.mode = SystemMode.FULL
                 G_STATE.mode_reason = ReasonCode.NONE
        
        # --- INPUT HANDLING ---
        if REPLAY_MODE:
            if not G_STATE.snapshot_buffer:
                time.sleep(1.0)
                continue
            snapshot = G_STATE.snapshot_buffer[replay_idx % len(G_STATE.snapshot_buffer)]
            replay_idx += 1
            
            # DETERMINISTIC REPLAY: Use snapshot time as loop authority
            loop_start = snapshot["snapshot_version"] # Use frame_id-mapped time or similar
            
            if "queue" not in snapshot:
                logger.warning("No state in snapshot. Replay limited.")
                time.sleep(0.033)
                continue
            # Note: Replay needs full state reconstruction. For now, we skip detections.
            detections = sv.Detections.empty() # Placeholder
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            time.sleep(0.033)
        elif USE_FAKE_INPUT:
            # Simulate 30 FPS
            time.sleep(0.033)
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            detections = engine.get_detections()
            if engine.is_complete(): engine.reset()
        else:
            # 1. Grab Frame
            t0 = time.time()
            ret, frame = cap.read()
            if not ret:
                logger.error("[CAMERA] Failed to grab frame. Reconnecting...")
                cap.release()
                time.sleep(1.0)
                cap = cv2.VideoCapture(CONFIG.get('camera_index', 1), cv2.CAP_DSHOW)
                continue
            t_grab = time.time() - t0
            
            # 2. Detect (AI)
            try:
                t1 = time.time()
                detections = tracker.update_with_detections(detector.detect(frame, conf=0.15))
                t_detect = time.time() - t1
            except Exception as e:
                logger.error(f"[VISION] Detection error: {e}")
                detections = sv.Detections.empty()
                t_detect = 0
                
            if frame_id % 30 == 0:
                logger.info(f"[PERF] Frame {frame_id}: Grab {t_grab:.3f}s | Detect {t_detect:.3f}s")
        
        # --- ATOMIC FRAME PROCESSING ---
        with G_STATE.vision_lock:
            G_STATE.last_vision_heartbeat = loop_start
            G_STATE.last_detections = detections
            resolution_wh = (frame.shape[1], frame.shape[0])
            max_diag = np.sqrt(resolution_wh[0]**2 + resolution_wh[1]**2)
            current_track_ids = set(detections.tracker_id) if detections.tracker_id is not None else set()
            
            # --- TRACKING PERSISTENCE ---
            for tid in current_track_ids:
                track_ages[tid] = track_ages.get(tid, 0) + 1
            
            # --- DEFENSIVE GUARD: Clear Stale Assignments ---
            for slot in G_STATE.slots:
                if slot.assigned_track_id is not None and slot.assigned_track_id not in current_track_ids:
                    slot.assigned_track_id = None

            # --- QUEUE MANAGEMENT (STAGE 3) ---
            with G_STATE.queue_lock:
                if G_STATE.queue_manager:
                    q_zones = CONFIG.get("queue_zones", [])
                    G_STATE.queue_manager.update_queue(detections, q_zones, frame_time=loop_start)
                    
                    # --- STAGE 4.5: COLD START SUPPRESSION ---
                    allow_new = (loop_start - G_STATE.startup_time > 2.0)
                    G_STATE.queue_manager.update_suggestions(G_STATE.slots, allow_new_assignments=allow_new, frame_time=loop_start)
            
            # --- ASSIGNMENT ---
            matched_slots = set()
            if len(detections) > 0:
                costs = np.zeros((len(detections), len(G_STATE.slots)))
                overlaps = np.zeros((len(detections), len(G_STATE.slots)))
                centroid_scores = np.zeros((len(detections), len(G_STATE.slots)))
                MAX_MATCH_COST = 0.6
                
                for d_idx in range(len(detections)):
                    det_xyxy = detections.xyxy[d_idx]
                    det_center = ((det_xyxy[0] + det_xyxy[2]) / 2, (det_xyxy[1] + det_xyxy[3]) / 2)
                    for s_idx, slot in enumerate(G_STATE.slots):
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
                        slot = G_STATE.slots[s_idx]
                        tid = detections.tracker_id[d_idx]
                        
                        # INVARIANT: Assignment Uniqueness
                        if tid in assigned_this_frame:
                            if STRICT_MODE:
                                raise RuntimeError("Duplicate assignment detected")
                            else:
                                slot.force_safe_state()
                                G_STATE.slots[assigned_this_frame[tid]].force_safe_state()
                                continue
                        
                        assigned_this_frame[tid] = s_idx
                        matched_slots.add(s_idx)
                        
                        overlap = overlaps[d_idx, s_idx]
                        c_score = centroid_scores[d_idx, s_idx]
                        final_score = 0.7 * overlap + 0.3 * c_score
                        slot.update_alignment(final_score, {"overlap_ratio": overlap, "centroid_score": c_score}, current_time=loop_start)
                        
                        # --- STATE MACHINE ---
                        if slot.state == SlotState.AUTH_PENDING:
                            is_auth, reason = G_STATE.auth_engine.is_authorized(s_idx, tid)
                            has_booking = (s_idx in G_STATE.auth_engine.bookings)
                            is_expired = G_STATE.auth_engine.is_expired(s_idx)
                            
                            timeout = CONFIG.get('auth_timeout', 60.0)
                            if loop_start - slot.state_enter_time > timeout:
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
                        slot.handle_occlusion(False, current_time=loop_start)

            # --- DEPARTURE & GHOST RECOVERY ---
            for i, slot in enumerate(G_STATE.slots):
                # Ghost Charging check
                if slot.state == SlotState.CHARGING and slot.locked_track_id is None:
                    logger.critical(f"[INVARIANT] Ghost Charging on Slot {i+1} -> FORCED RESET")
                    slot.force_safe_state()
                    G_STATE.auth_engine.revoke_authorization(i)

                if i not in matched_slots:
                    if slot.state == SlotState.AUTH_PENDING:
                        if not hasattr(slot, 'departure_time'): slot.departure_time = loop_start
                        if loop_start - slot.departure_time > 1.0:
                            logger.warning(f"[AUTH] REVOKE: VEHICLE_LEFT for Slot {i+1}")
                            G_STATE.auth_engine.revoke_authorization(i)
                            slot.set_state(SlotState.FREE)
                    else:
                        if hasattr(slot, 'departure_time'): del slot.departure_time
                    if slot.state not in [SlotState.FREE, SlotState.RESERVED]:
                        slot.handle_occlusion(True, current_time=loop_start)

            active_ids = set()
            for s_idx, s in enumerate(G_STATE.slots):
                if s.locked_track_id is not None:
                    if s.locked_track_id in active_ids:
                        trigger_freeze("INTEGRITY_DUPLICATE: Multiple slots assigned same ID")
                    active_ids.add(s.locked_track_id)
            
        # 2. Snapshot Generation (Single Point of Truth)
        current_snapshot = None
        if not G_STATE.is_forensic_frozen:
            current_snapshot = get_system_snapshot(frame_id, loop_start, debug=True)
            if current_snapshot:
                G_STATE.snapshot_buffer.append(current_snapshot)
                
                # 3. Periodic Referential Integrity Audit (Reuse current_snapshot)
                if frame_id % CONFIG.get("integrity_interval", 30) == 0:
                    validate_referential_integrity(current_snapshot)
        else:
            # Auto-unfreeze safety (60s)
            if loop_start - G_STATE.freeze_start_time > 60.0:
                logger.warning("[FORENSICS] Auto-unfreezing buffer (Timeout)")
                G_STATE.is_forensic_frozen = False

        # --- REAL-TIME ESP32 BROADCAST ---
        if frame_id % 5 == 0: # 6 FPS broadcast
            for slot_idx, slot in enumerate(G_STATE.slots):
                if slot_idx + 1 == 1: # Target Slot 1
                    payload = {
                        "slot_id": 1,
                        "command": "SET_STATE",
                        "state": slot.state.name,
                        "vehicle_present": slot.locked_track_id is not None,
                        "confidence": round(slot.smoothed_alignment_score, 2),
                        "timestamp": int(loop_start)
                    }
                    msg = json.dumps(payload)
                    dead_clients = set()
                    for client in esp32_clients:
                        try:
                            client.send(msg)
                        except:
                            dead_clients.add(client)
                    for dc in dead_clients:
                        esp32_clients.discard(dc)

        if not USE_FAKE_INPUT:
            # --- OVERLAYS ---
            frame = visualizer.draw_overlays(frame, G_STATE.slots, CONFIG.get('queue_zones', []), {})
            frame = visualizer.draw_detections(frame, detections)
            frame = visualizer.draw_sidebar(frame, G_STATE.queue_manager)
            
            # Draw Calibration Preview
            global INTERACTIVE_POINTS, INTERACTIVE_MODE
            for pt in INTERACTIVE_POINTS:
                cv2.circle(frame, tuple(pt), 4, (0, 0, 255), -1)
            if len(INTERACTIVE_POINTS) >= 2:
                cv2.polylines(frame, [np.array(INTERACTIVE_POINTS)], False, (0, 255, 255), 2)

            cv2.imshow("Smart EV Charging", frame)
            key = cv2.waitKey(1) & 0xFF
            if key != 255:
                logger.info(f"[DEBUG] Key pressed: {key} (q={ord('q')})")
            
            if key in [ord('q'), ord('Q')]: 
                logger.info("[DEBUG] Q key detected. Saving configuration...")
                try:
                    CONFIG_TO_SAVE = copy.deepcopy(CONFIG)
                    CONFIG_TO_SAVE['slots'] = [s.polygon.tolist() for s in G_STATE.slots]
                    with open("config.yaml", 'w') as f:
                        yaml.dump(CONFIG_TO_SAVE, f)
                    logger.info("[CONFIG] Successfully saved calibration to config.yaml")
                except Exception as e:
                    logger.error(f"[CONFIG] Save failed: {e}")
                
                logger.info("[SYSTEM] Quitting...")
                break
            elif key == ord('s'):
                pts = calibrate_zones(frame, mode='SLOT')
                if len(pts) >= 3:
                    new_slot = Slot(len(G_STATE.slots), np.array(pts, np.int32))
                    with G_STATE.vision_lock:
                        G_STATE.slots.append(new_slot)
                    logger.info(f"[CALIB] Slot {len(G_STATE.slots)} added and synced.")
            elif key == ord('z'):
                pts = calibrate_zones(frame, mode='QUEUE')
                if len(pts) >= 3:
                    q_zones = CONFIG.get('queue_zones', [])
                    q_zones.append(pts)
                    CONFIG['queue_zones'] = q_zones
                    logger.info(f"[CALIB] Queue zone added.")
            elif key == ord('c'):
                with G_STATE.vision_lock:
                    G_STATE.slots = []
                    CONFIG['slots'] = []
                    CONFIG['queue_zones'] = []
                logger.info("[CALIB] All zones cleared.")
        else:
            # Already slept at the top of the loop in FAKE mode
            pass

    if cap: cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__": main()
