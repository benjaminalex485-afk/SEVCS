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
DEV_MODE = utils.DEV_MODE
ALIGN_THRESHOLD = 0.75 if not DEV_MODE else 0.3
AUTH_WINDOW = 1.0 if not DEV_MODE else 10.0 # Relaxed early authorization window
STRICT_MODE = CONFIG.get("strict_mode", False) if not DEV_MODE else False
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
            {"username": "user", "password": "user", "role": "user"},
            {"username": "test3@gmail.com", "password": "123", "role": "user"},
            {"username": "test4@gmail.com", "password": "123", "role": "user"}
        ]
        self.wallets = collections.defaultdict(lambda: {"balance": 0.0, "currency": "USD"})
        # Pre-seed some balance for testing
        self.wallets["test3@gmail.com"]["balance"] = 100.0
        self.pricing_quotes = {}
        self.payment_receipts = {}
        # (slot_id|date|time_window) -> booking metadata
        self.slot_time_reservations = {}

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


def select_zones(cap, title="Selection Mode", current_zones=None, mode="slot"):
    """
    Opens an interactive window to define polygons.
    mode: "slot" (Green) or "queue" (Blue)
    Matches earlier implementation in reference/src/utils.py
    """
    if current_zones is None:
        current_zones = []
    
    # Freeze the frame for calibration
    ret, frame = cap.read()
    if not ret:
        logger.error("[CALIB] Could not read from camera for calibration.")
        return current_zones
    
    temp_zones = copy.deepcopy(current_zones)
    current_polygon = []
    
    window_name = title
    cv2.namedWindow(window_name)

    def mouse_callback(event, x, y, flags, param):
        nonlocal current_polygon
        if event == cv2.EVENT_LBUTTONDOWN:
            current_polygon.append([x, y])
            logger.info(f"[CALIB] Point added: ({x}, {y})")
        elif event == cv2.EVENT_RBUTTONDOWN:
            if len(current_polygon) > 2:
                temp_zones.append(current_polygon.copy())
                current_polygon = []
                logger.info(f"[CALIB] Polygon completed. Total zones: {len(temp_zones)}")

    cv2.setMouseCallback(window_name, mouse_callback)

    color_map = {"slot": (0, 255, 0), "queue": (255, 0, 0)} # Green, Blue
    draw_color = color_map.get(mode.lower(), (0, 255, 0))

    while True:
        display_frame = frame.copy()
        
        # Draw existing zones
        for zone in temp_zones:
            pts = np.array(zone, np.int32).reshape((-1, 1, 2))
            cv2.polylines(display_frame, [pts], isClosed=True, color=draw_color, thickness=2)

        # Draw current polygon in progress
        if len(current_polygon) > 0:
            pts = np.array(current_polygon, np.int32).reshape((-1, 1, 2))
            cv2.polylines(display_frame, [pts], isClosed=False, color=(0, 0, 255), thickness=1)
            for pt in current_polygon:
                cv2.circle(display_frame, tuple(pt), 3, (0, 0, 255), -1)

        # Instructions overlay
        cv2.putText(display_frame, f"MODE: {mode.upper()}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, draw_color, 2)
        cv2.putText(display_frame, "L-Click: Add Point | R-Click: Close Polygon", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(display_frame, "'z': Undo | 'c': Clear | 'q'/ESC: Save & Quit", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        cv2.imshow(window_name, display_frame)
        key = cv2.waitKey(1) & 0xFF

        if key == ord('q') or key == 27: # ESC
            if len(current_polygon) > 2:
                 temp_zones.append(current_polygon)
            break
        elif key == ord('z'):
            if current_polygon:
                current_polygon.pop()
            elif temp_zones:
                temp_zones.pop()
        elif key == ord('c'):
            temp_zones = []
            current_polygon = []

    cv2.destroyWindow(window_name)
    return temp_zones

def trigger_freeze(reason):
    """Idempotent freeze trigger with priority and diagnostic logging."""
    if G_STATE.is_forensic_frozen:
        # Only override if new reason is critical or we are in the first frame of freeze
        return
    
    if DEV_MODE:
        logger.warning(f"[DEV MODE] System freeze BYPASSED: {reason}")
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
            "system_mode": local_mode.name,
            "dev_mode": DEV_MODE
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
            if DEV_MODE:
                logger.warning("[DEV MODE] EMPTY_SLOTS detected in snapshot. Continuing.")
            else:
                raise ValueError("EMPTY_SLOTS")

        # 2. Mode must be present
        mode = normalized.get("mode")
        if mode is None:
            if DEV_MODE:
                logger.warning("[DEV MODE] MISSING_MODE detected in snapshot. Continuing.")
            else:
                raise ValueError("MISSING_MODE")

        # 3. Queue can be empty ONLY in safe/idle states
        queue = normalized.get("queue")
        if queue is None:
            if DEV_MODE:
                logger.warning("[DEV MODE] MISSING_QUEUE detected in snapshot. Continuing.")
            else:
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
    if not request.is_json:
        return {}
    data = request.get_json(silent=True) or {}
    if "payload" in data:
        return data["payload"]
    return data

def _get_slot_charger_type(slot):
    return getattr(slot, "charger_type", "STANDARD")

def _get_time_multiplier(time_window):
    if not time_window:
        return 1.0
    hour_map = {
        "00:00-06:00": 0.8,
        "06:00-12:00": 1.0,
        "12:00-18:00": 1.2,
        "18:00-24:00": 1.4
    }
    return float(hour_map.get(time_window, 1.0))

def _reservation_key(slot_id, date, time_window):
    return f"{int(slot_id)}|{str(date)}|{str(time_window)}"

def _current_date_str():
    return time.strftime("%Y-%m-%d", time.localtime())

def _current_time_window():
    hour = time.localtime().tm_hour
    if 0 <= hour < 6:
        return "00:00-06:00"
    if 6 <= hour < 12:
        return "06:00-12:00"
    if 12 <= hour < 18:
        return "12:00-18:00"
    return "18:00-24:00"

def _estimate_slot_eta_minutes(slot_idx):
    # Estimate remaining charging time from active session if present.
    session = G_STATE.sessions.get(slot_idx)
    if not session:
        return 30
    battery_pct = float(session.get("battery_pct", 0))
    remaining = max(0.0, 100.0 - battery_pct)
    # ~0.1% per second in simulation loop => 60% in 10 minutes.
    return max(1, int(round((remaining / 0.1) / 60.0)))

def _build_user_active_sessions(username):
    sessions = []
    with G_STATE.session_lock, G_STATE.auth_engine.lock:
        for slot_idx, session in G_STATE.sessions.items():
            booking = G_STATE.auth_engine.bookings.get(slot_idx, {})
            if booking.get("user") != username:
                continue
            sessions.append({
                "slot_id": int(slot_idx),
                "battery_pct": round(float(session.get("battery_pct", 0.0)), 2),
                "power_kw": round(float(session.get("power", 0.0)), 2),
                "energy_kwh": round(float(session.get("energy", 0.0)), 2),
                "started_at": float(session.get("start_time", 0.0))
            })
    sessions.sort(key=lambda x: x.get("started_at", 0.0), reverse=True)
    return sessions

def _build_admin_kpis(now_ts):
    day_ago = float(now_ts) - 86400.0
    with G_STATE.vision_lock, G_STATE.session_lock:
        receipts = list(G_STATE.payment_receipts.values())
        quotes = G_STATE.pricing_quotes

        def _receipt_amount(receipt):
            amount = receipt.get("amount")
            if amount is not None:
                try:
                    return float(amount)
                except (TypeError, ValueError):
                    pass
            quote_id = receipt.get("quote_id")
            q = quotes.get(quote_id, {})
            return float(q.get("total_price", 0.0))

        def _receipt_energy(receipt):
            quote_id = receipt.get("quote_id")
            q = quotes.get(quote_id, {})
            return float(q.get("requested_kwh", 0.0))

        def _aggregate(bucket):
            session_count = len(bucket)
            revenue = round(sum(_receipt_amount(r) for r in bucket), 2)
            energy = round(sum(_receipt_energy(r) for r in bucket), 2)
            avg_session_value = round((revenue / session_count), 2) if session_count > 0 else 0.0
            avg_kwh_per_session = round((energy / session_count), 2) if session_count > 0 else 0.0
            return {
                "total_revenue": revenue,
                "total_energy_kwh": energy,
                "session_count": session_count,
                "avg_session_value": avg_session_value,
                "avg_kwh_per_session": avg_kwh_per_session
            }

        receipts_24h = [r for r in receipts if float(r.get("processed_at", 0.0)) >= day_ago]
        return {
            "last24h": _aggregate(receipts_24h),
            "lifetime": _aggregate(receipts)
        }

def build_degraded_status_snapshot(username, reason):
    """Always return a schema-compatible status payload for UI safety."""
    now = utils.system_now(caller="api_thread")
    mode = "WAITING_FOR_CAMERA" if not G_STATE.camera_online else "INITIALIZING"
    return {
        "snapshot_sequence": int(G_STATE.snapshot_sequence),
        "snapshot_version": int(G_STATE.last_snapshot_version),
        "previous_snapshot_version": int(G_STATE.last_snapshot_version),
        "timestamp": now,
        "source": "BACKEND",
        "mode": mode,
        "system_mode": mode,
        "mode_reason": str(reason),
        "system_health": 0.0 if not G_STATE.camera_online else 25.0,
        "freeze_state": bool(G_STATE.is_forensic_frozen),
        "freeze_reason": str(G_STATE.freeze_reason) if G_STATE.freeze_reason else "",
        "state_hash": "",
        "schema_version": int(G_STATE.schema_version),
        "slots": [],
        "queue": [],
        "user_id": username,
        "user_wallet": G_STATE.wallets.get(username, {"balance": 0.0, "currency": "USD"}),
        "user_bookings": [],
        "user_active_sessions": [],
        "admin_kpis": {
            "last24h": {
                "total_revenue": 0.0,
                "total_energy_kwh": 0.0,
                "session_count": 0,
                "avg_session_value": 0.0,
                "avg_kwh_per_session": 0.0
            },
            "lifetime": {
                "total_revenue": 0.0,
                "total_energy_kwh": 0.0,
                "session_count": 0,
                "avg_session_value": 0.0,
                "avg_kwh_per_session": 0.0
            }
        },
        "dev_mode": DEV_MODE
    }

def build_pipeline_placeholder_snapshot(frame_id, frame_time, reason):
    """Emit a valid placeholder snapshot when pipeline is degraded/frozen."""
    with G_STATE.snapshot_lock:
        G_STATE.snapshot_sequence += 1
        seq = G_STATE.snapshot_sequence
        prev = G_STATE.last_snapshot_version
        G_STATE.last_snapshot_version = frame_id

    mode = "DEGRADED" if not G_STATE.is_forensic_frozen else "FROZEN"
    return {
        "snapshot_sequence": int(seq),
        "snapshot_version": int(frame_id),
        "previous_snapshot_version": int(prev if prev else frame_id),
        "timestamp": utils.system_now(caller="main_loop"),
        "frame_time": utils.normalize_float(frame_time),
        "source": "BACKEND",
        "mode": mode,
        "system_mode": mode,
        "mode_reason": str(reason),
        "system_health": 0.0,
        "freeze_state": bool(G_STATE.is_forensic_frozen),
        "freeze_reason": str(G_STATE.freeze_reason) if G_STATE.freeze_reason else "",
        "state_hash": "",
        "schema_version": int(G_STATE.schema_version),
        "slots": [],
        "queue": [],
        "dev_mode": DEV_MODE
    }

# --- API SERVER ---
@api_app.route('/api/status', methods=['GET'])
def get_status():
    username = request.args.get('username', 'Anonymous')
    now = utils.system_now(caller="api_thread")
    status_mode = "INITIALIZING"
    if G_STATE.snapshot_buffer:
        snapshot = copy.deepcopy(G_STATE.snapshot_buffer[-1])
        with G_STATE.vision_lock:
            user_bookings = [
                {
                    "slot_id": int(res.get("slot_id", -1)),
                    "date": res.get("date"),
                    "time_window": res.get("time_window"),
                    "auth_code": res.get("auth_code", "N/A"),
                    "created_at": float(res.get("created_at", 0.0))
                }
                for res in G_STATE.slot_time_reservations.values()
                if res.get("username") == username
            ]
        user_bookings.sort(key=lambda x: x.get("created_at", 0.0), reverse=True)
        # Inject the correct wallet and user ID for this specific session
        snapshot["user_id"] = username
        snapshot["user_wallet"] = G_STATE.wallets.get(username, {"balance": 0.0, "currency": "USD"})
        snapshot["user_bookings"] = user_bookings
        snapshot["user_active_sessions"] = _build_user_active_sessions(username)
        snapshot["admin_kpis"] = _build_admin_kpis(now)
        # Keep producer timestamp untouched for frontend freshness logic.
        snapshot["api_timestamp"] = now
        snapshot["dev_mode"] = DEV_MODE
        status_mode = snapshot.get("system_mode", "LIVE")
        if not hasattr(get_status, "_last_status_log"):
            get_status._last_status_log = 0.0
        if now - get_status._last_status_log >= 2.0:
            get_status._last_status_log = now
            logger.info(
                "[API_STATUS] mode=%s seq=%s age=%.2fs slots=%d queue=%d",
                status_mode,
                snapshot.get("snapshot_sequence", -1),
                max(0.0, now - float(snapshot.get("timestamp", now))),
                len(snapshot.get("slots", [])),
                len(snapshot.get("queue", [])),
            )
        return jsonify(snapshot)
    reason = "Camera unavailable (degraded mode)" if not G_STATE.camera_online else "State initialized"
    payload = build_degraded_status_snapshot(username, reason)
    status_mode = payload.get("system_mode", "INITIALIZING")
    if not hasattr(get_status, "_last_status_log"):
        get_status._last_status_log = 0.0
    if now - get_status._last_status_log >= 2.0:
        get_status._last_status_log = now
        logger.info("[API_STATUS] mode=%s seq=%s cold_start=1", status_mode, payload.get("snapshot_sequence", -1))
    return jsonify(payload), 200

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
            if DEV_MODE:
                return f(*args, **kwargs)
            
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

@api_app.route('/api/debug/health', methods=['GET'])
def debug_health_api():
    now = utils.system_now(caller="api_thread")
    return jsonify({
        "source": "BACKEND",
        "camera_online": bool(G_STATE.camera_online),
        "vision_heartbeat_age_s": round(max(0.0, now - G_STATE.last_vision_heartbeat), 3),
        "snapshot_available": bool(len(G_STATE.snapshot_buffer) > 0),
        "mode": G_STATE.mode.name,
        "freeze_state": bool(G_STATE.is_forensic_frozen),
        "freeze_reason": G_STATE.freeze_reason
    })

@api_app.route('/api/debug/snapshot_meta', methods=['GET'])
def debug_snapshot_meta_api():
    if G_STATE.snapshot_buffer:
        latest = G_STATE.snapshot_buffer[-1]
        return jsonify({
            "source": "BACKEND",
            "snapshot_sequence": latest.get("snapshot_sequence", -1),
            "snapshot_version": latest.get("snapshot_version", -1),
            "timestamp": latest.get("timestamp", 0),
            "slots_count": len(latest.get("slots", [])),
            "queue_count": len(latest.get("queue", []))
        })
    return jsonify({
        "source": "BACKEND",
        "snapshot_sequence": -1,
        "snapshot_version": -1,
        "timestamp": utils.system_now(caller="api_thread"),
        "slots_count": 0,
        "queue_count": 0
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
    data = get_request_data()
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
    data = get_request_data()
    username = data.get('username', 'Anonymous')
    identifier = f"{request.remote_addr}:{username}"
    reservation_key = None
    
    if not DEV_MODE:
        limit = CONFIG.get('rate_limit_attempts', 5)
        window = CONFIG.get('rate_limit_window', 60.0)
        if not G_STATE.auth_engine.check_rate_limit(identifier, limit, window):
            return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
        G_STATE.auth_engine.record_attempt(identifier)

    quote_id = data.get("quote_id")
    if quote_id:
        with G_STATE.vision_lock:
            quote = G_STATE.pricing_quotes.get(quote_id)
            if not quote:
                return jsonify({"status": "error", "message": "Invalid quote_id"}), 400
            if quote.get("username") != username:
                return jsonify({"status": "error", "message": "Quote user mismatch"}), 400
            if utils.system_now(caller="api_thread") > quote.get("expires_at", 0):
                return jsonify({"status": "error", "message": "Quote expired"}), 400
            if not quote.get("paid", False):
                return jsonify({"status": "error", "message": "Payment required before booking"}), 400
            if quote.get("consumed", False):
                return jsonify({"status": "error", "message": "Quote already used"}), 409
            reservation_key = _reservation_key(quote.get("slot_id"), quote.get("date"), quote.get("time_window"))
            existing_res = G_STATE.slot_time_reservations.get(reservation_key)
            if existing_res and existing_res.get("quote_id") != quote_id:
                return jsonify({"status": "error", "message": "Slot already booked for selected date/time"}), 409

    with G_STATE.vision_lock:
        assigned_idx = -1
        # Use provided slot_id if available, else find one
        req_slot_id = data.get('slot_id')
        if req_slot_id is not None:
            try:
                raw_slot_id = int(req_slot_id)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "Invalid slot_id"}), 400
            # Accept both 0-based (UI cards: Slot 0..N-1) and 1-based ids.
            if 0 <= raw_slot_id < len(G_STATE.slots):
                assigned_idx = raw_slot_id
            elif 1 <= raw_slot_id <= len(G_STATE.slots):
                assigned_idx = raw_slot_id - 1
            else:
                return jsonify({"status": "error", "message": "Slot not found"}), 404
            logger.info(f"[BOOKING] Manual booking request for Slot {assigned_idx+1}")
        else:
            # AUTO-FIND logic
            for i, slot in enumerate(G_STATE.slots):
                if slot.locked_track_id is not None:
                    if i not in G_STATE.auth_engine.bookings or G_STATE.auth_engine.is_expired(i):
                        assigned_idx = i
                        break
            if assigned_idx == -1:
                for i, slot in enumerate(G_STATE.slots):
                    if slot.state == SlotState.FREE and (i not in G_STATE.auth_engine.bookings or G_STATE.auth_engine.is_expired(i)):
                        assigned_idx = i
                        break
        
    if assigned_idx != -1 and assigned_idx < len(G_STATE.slots):
        timeout = data.get('timeout', 600)
        code = G_STATE.auth_engine.generate_booking(assigned_idx, username, timeout=timeout)
        if not code:
            return jsonify({"status": "error", "message": "Booking rejected for this slot"}), 409
        if quote_id:
            quote["consumed"] = True
            G_STATE.slot_time_reservations[reservation_key] = {
                "quote_id": quote_id,
                "username": username,
                "slot_id": assigned_idx,
                "date": quote.get("date"),
                "time_window": quote.get("time_window"),
                "auth_code": code,
                "created_at": utils.system_now(caller="api_thread")
            }
        return jsonify({"status": "success", "slot_id": assigned_idx + 1, "auth_code": code})
    
    return jsonify({"status": "error", "message": "No slots available"}), 400

@api_app.route('/api/availability', methods=['GET'])
def availability_api():
    username = request.args.get("username", "Anonymous")
    with G_STATE.vision_lock:
        slots = [
            {
                "slot_id": slot.slot_id,
                "charger_type": _get_slot_charger_type(slot),
                "state": slot.state.name
            }
            for slot in G_STATE.slots if slot.state == SlotState.FREE
        ]
    logger.info(f"[CHARGE_FLOW] availability_requested user={username} free_slots={len(slots)}")
    return jsonify({
        "status": "success",
        "slots": slots,
        "generated_at": utils.system_now(caller="api_thread")
    })

@api_app.route('/api/pricing_quote', methods=['POST'])
def pricing_quote_api():
    data = get_request_data()
    username = data.get("username", "Anonymous")
    slot_id = data.get("slot_id")
    date = data.get("date")
    time_window = data.get("time_window")
    requested_kwh = data.get("requested_kwh", 20)
    charge_rate_kw = data.get("charge_rate_kw", 7)
    allow_waitlist = bool(data.get("allow_waitlist", False))
    if slot_id is None or not date or not time_window:
        return jsonify({"status": "error", "message": "slot_id, date, and time_window are required"}), 400
    try:
        slot_idx = int(slot_id)
        requested_kwh = float(requested_kwh)
        charge_rate_kw = float(charge_rate_kw)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid slot_id/requested_kwh/charge_rate_kw"}), 400
    requested_kwh = max(5.0, min(120.0, requested_kwh))
    charge_rate_kw = max(3.0, min(150.0, charge_rate_kw))

    with G_STATE.vision_lock:
        slot = next((s for s in G_STATE.slots if s.slot_id == slot_idx), None)
        if not slot:
            return jsonify({"status": "error", "message": "Slot not found"}), 404
        if slot.state != SlotState.FREE and not allow_waitlist:
            return jsonify({"status": "error", "message": "Slot not available"}), 409
        reservation_key = _reservation_key(slot_idx, date, time_window)
        if reservation_key in G_STATE.slot_time_reservations:
            return jsonify({"status": "error", "message": "Slot already booked for selected date/time"}), 409
        charger_type = _get_slot_charger_type(slot)

    base_price = 10.0 if charger_type == "STANDARD" else 18.0
    multiplier = _get_time_multiplier(time_window)
    energy_factor = requested_kwh / 20.0
    rate_factor = 1.0 + ((charge_rate_kw - 7.0) / 100.0)
    total_price = round(base_price * multiplier * energy_factor * rate_factor, 2)
    quote_id = f"q_{int(utils.system_now(caller='api_thread')*1000)}_{slot_idx}"
    expires_at = utils.system_now(caller="api_thread") + 300.0
    quote_payload = {
        "quote_id": quote_id,
        "username": username,
        "slot_id": slot_idx,
        "charger_type": charger_type,
        "date": date,
        "time_window": time_window,
        "unit_price": base_price,
        "multiplier": multiplier,
        "requested_kwh": requested_kwh,
        "charge_rate_kw": charge_rate_kw,
        "total_price": total_price,
        "currency": "USD",
        "expires_at": expires_at,
        "paid": False,
        "consumed": False,
        "allow_waitlist": allow_waitlist
    }
    G_STATE.pricing_quotes[quote_id] = quote_payload
    logger.info(f"[CHARGE_FLOW] quote_generated quote_id={quote_id} slot={slot_idx} total={total_price}")
    return jsonify({"status": "success", **quote_payload})

@api_app.route('/api/payment/mock', methods=['POST'])
def payment_mock_api():
    data = get_request_data()
    quote_id = data.get("quote_id")
    username = data.get("username", "Anonymous")
    method = data.get("method", "WALLET")
    if not quote_id:
        return jsonify({"status": "error", "message": "quote_id is required"}), 400
    with G_STATE.vision_lock:
        quote = G_STATE.pricing_quotes.get(quote_id)
        if not quote:
            return jsonify({"status": "error", "message": "Quote not found"}), 404
        if quote.get("username") != username:
            return jsonify({"status": "error", "message": "Quote user mismatch"}), 400
        if utils.system_now(caller="api_thread") > quote.get("expires_at", 0):
            return jsonify({"status": "error", "message": "Quote expired"}), 400
        if quote.get("consumed", False):
            return jsonify({"status": "error", "message": "Quote already used"}), 409
        if quote.get("paid", False):
            return jsonify({"status": "error", "message": "Quote already paid"}), 409
        reservation_key = _reservation_key(quote.get("slot_id"), quote.get("date"), quote.get("time_window"))
        existing_res = G_STATE.slot_time_reservations.get(reservation_key)
        if existing_res and existing_res.get("quote_id") != quote_id:
            return jsonify({"status": "error", "message": "Slot already booked for selected date/time"}), 409
        total_price = float(quote.get("total_price", 0.0))
        wallet = G_STATE.wallets[username]
        balance = float(wallet.get("balance", 0.0))
        if balance < total_price:
            return jsonify({
                "status": "error",
                "message": f"Insufficient wallet balance. Required ${total_price:.2f}, available ${balance:.2f}"
            }), 400
        wallet["balance"] = round(balance - total_price, 2)
        payment_id = f"pay_{int(utils.system_now(caller='api_thread')*1000)}"
        quote["paid"] = True
    receipt = {
        "status": "success",
        "payment_id": payment_id,
        "quote_id": quote_id,
        "method": method,
        "amount": total_price,
        "wallet_balance": wallet["balance"],
        "processed_at": utils.system_now(caller="api_thread")
    }
    G_STATE.payment_receipts[payment_id] = receipt
    G_STATE.last_snapshot_version += 1
    logger.info(f"[CHARGE_FLOW] payment_wallet_result payment_id={payment_id} quote_id={quote_id} user={username} amount={total_price} balance={wallet['balance']}")
    return jsonify(receipt)

@api_app.route('/api/admin_add_slot', methods=['POST'])
def admin_add_slot_api():
    data = get_request_data()
    charger_type = (data.get('charger_type') or "STANDARD").upper()
    with G_STATE.vision_lock:
        slot_id = len(G_STATE.slots)
        # Use the last polygon as a placeholder for debug/admin runtime behavior.
        polygon = G_STATE.slots[-1].polygon if G_STATE.slots else np.array([[50, 50], [250, 50], [250, 250], [50, 250]], np.int32)
        G_STATE.slots.append(Slot(slot_id, polygon))
    logger.info(f"[ADMIN] Added slot {slot_id + 1} (type={charger_type})")
    return jsonify({"status": "success", "slot_id": slot_id + 1, "charger_type": charger_type})

@api_app.route('/api/admin_remove_slot', methods=['POST'])
def admin_remove_slot_api():
    data = get_request_data()
    try:
        target_slot = int(data.get('slot_id')) - 1
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid slot_id"}), 400
    with G_STATE.vision_lock:
        if target_slot < 0 or target_slot >= len(G_STATE.slots):
            return jsonify({"status": "error", "message": "Slot not found"}), 404
        G_STATE.slots.pop(target_slot)
        # Reindex for predictable UI behavior
        for idx, slot in enumerate(G_STATE.slots):
            slot.slot_id = idx
    logger.info(f"[ADMIN] Removed slot {target_slot + 1}")
    return jsonify({"status": "success"})

@api_app.route('/api/admin_update_slot_type', methods=['POST'])
def admin_update_slot_type_api():
    data = get_request_data()
    try:
        target_slot = int(data.get('slot_id')) - 1
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid slot_id"}), 400
    charger_type = (data.get('charger_type') or "STANDARD").upper()
    with G_STATE.vision_lock:
        if target_slot < 0 or target_slot >= len(G_STATE.slots):
            return jsonify({"status": "error", "message": "Slot not found"}), 404
        setattr(G_STATE.slots[target_slot], "charger_type", charger_type)
    logger.info(f"[ADMIN] Updated slot {target_slot + 1} type to {charger_type}")
    return jsonify({"status": "success", "slot_id": target_slot + 1, "charger_type": charger_type})

@api_app.route('/api/recharge', methods=['POST'])
def recharge_api():
    inner_data = get_request_data()
    username = inner_data.get('username', "Anonymous")
    amount = float(inner_data.get('amount', 0))
    
    if amount <= 0:
        return jsonify({"status": "error", "message": "Invalid amount"}), 400
        
    G_STATE.wallets[username]["balance"] += amount
    logger.info(f"[WALLET] User {username} recharged ${amount}. New balance: ${G_STATE.wallets[username]['balance']}")
    
    # Force a version bump so UI sees the change immediately
    G_STATE.last_snapshot_version += 1
    return jsonify({"status": "success", "balance": G_STATE.wallets[username]["balance"]})

@api_app.route('/api/find_slot', methods=['POST'])
def find_slot_api():
    data = get_request_data()
    v_type = data.get('type', 'SUV')
    urgency = data.get('urgency', 'LOW')
    date = data.get("date") or _current_date_str()
    time_window = data.get("time_window") or _current_time_window()
    username = data.get("username", "Anonymous")

    with G_STATE.vision_lock:
        free_candidates = []
        busy_candidates = []
        for slot in G_STATE.slots:
            reservation_key = _reservation_key(slot.slot_id, date, time_window)
            if reservation_key in G_STATE.slot_time_reservations:
                continue
            if slot.state == SlotState.FREE:
                free_candidates.append(slot)
            else:
                busy_candidates.append(slot)

        if free_candidates:
            selected = sorted(free_candidates, key=lambda s: s.slot_id)[0]
            logger.info(f"[FIND_SLOT] immediate_available user={username} slot={selected.slot_id} date={date} tw={time_window} type={v_type} urgency={urgency}")
            return jsonify({
                "status": "success",
                "mode": "AVAILABLE",
                "message": f"Slot {selected.slot_id + 1} is available now.",
                "date": date,
                "time_window": time_window,
                "recommended_slot": {
                    "slot_id": selected.slot_id,
                    "charger_type": _get_slot_charger_type(selected),
                    "state": selected.state.name
                }
            })

        if busy_candidates:
            selected_busy = sorted(busy_candidates, key=lambda s: s.slot_id)[0]
            eta = _estimate_slot_eta_minutes(selected_busy.slot_id)
            logger.info(f"[FIND_SLOT] wait_required user={username} slot={selected_busy.slot_id} eta={eta}m date={date} tw={time_window} type={v_type} urgency={urgency}")
            return jsonify({
                "status": "success",
                "mode": "WAIT",
                "message": f"No slot is free right now. Earliest Slot {selected_busy.slot_id + 1} in about {eta} min.",
                "eta_minutes": eta,
                "date": date,
                "time_window": time_window,
                "recommended_slot": {
                    "slot_id": selected_busy.slot_id,
                    "charger_type": _get_slot_charger_type(selected_busy),
                    "state": selected_busy.state.name
                },
                "can_reserve_with_payment": True
            })

    logger.info(f"[FIND_SLOT] no_slot_found user={username} date={date} tw={time_window} type={v_type} urgency={urgency}")
    return jsonify({"status": "error", "message": "No slots available"}), 400

@api_app.route('/api/authorize', methods=['POST'])
def authorize_api():
    data = get_request_data()
    username = data.get("username", "Anonymous")
    identifier = f"{request.remote_addr}:{username}"

    if not DEV_MODE:
        limit = CONFIG.get('rate_limit_attempts', 5)
        window = CONFIG.get('rate_limit_window', 60.0)
        if not G_STATE.auth_engine.check_rate_limit(identifier, limit, window):
            return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
        G_STATE.auth_engine.record_attempt(identifier)

    slot_id, code = data.get('slot_id'), data.get('code')
    if slot_id is None or code is None:
        return jsonify({"status": "error", "message": "Missing slot_id or code"}), 400
    try:
        raw_slot_id = int(slot_id)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid slot_id"}), 400
    loop_start = utils.system_now(caller="api_thread")
    
    with G_STATE.vision_lock:
        if 0 <= raw_slot_id < len(G_STATE.slots):
            idx = raw_slot_id
        elif 1 <= raw_slot_id <= len(G_STATE.slots):
            idx = raw_slot_id - 1
        else:
            return jsonify({"status": "error", "code": "wrong_slot", "message": "Slot not found"}), 400
        if idx < 0 or idx >= len(G_STATE.slots): return jsonify({"status": "error", "code": "wrong_slot"}), 400
        slot = G_STATE.slots[idx]
        is_pending = (slot.state == SlotState.AUTH_PENDING)
        is_early_window = (slot.state == SlotState.ALIGNMENT_PENDING and (loop_start - slot.state_enter_time <= AUTH_WINDOW))
        if not (is_pending or is_early_window):
            return jsonify({"status": "error", "code": "stale_request", "message": "This slot is not ready for authorization yet"}), 400
        if slot.locked_track_id is None:
            return jsonify({"status": "error", "code": "no_vehicle", "message": "No vehicle detected in selected slot"}), 400
        if slot.is_in_occlusion_debounce():
            logger.warning(f"[AUTH] REJECTED Early Auth for Slot {idx+1}: UNSTABLE_TRACK (Occluded)")
            return jsonify({"status": "error", "code": "unstable_track", "message": "Vehicle tracking is unstable"}), 400
            
        current_track_id = slot.locked_track_id if slot.locked_track_id is not None else -1
        
    status_message_map = {
        "wrong_slot": "No booking found for this slot. Enter code on the booked slot.",
        "stale_request": "Authorization window is not active for this slot.",
        "no_vehicle": "No vehicle detected in this slot. Park correctly and retry.",
        "unstable_track": "Vehicle tracking unstable. Hold position and retry.",
        "expired": "Authorization code expired. Please book again.",
        "invalid_code": "Invalid authorization code.",
        "ID_MISMATCH": "This code belongs to a different vehicle/slot.",
    }
    status, is_idempotent = G_STATE.auth_engine.authorize_vehicle(idx, code, current_track_id)
    if status == "success":
        return jsonify({
            "status": "success",
            "idempotent": is_idempotent,
            "next_action": "START_CHARGING_CONFIRM",
            "slot_id": idx + 1
        })
    return jsonify({"status": "error", "code": status, "message": status_message_map.get(status, f"Authorization failed: {status}")}), 400

@api_app.route('/api/start_charging', methods=['POST'])
def start_charging_api():
    data = get_request_data()
    username = data.get("username", "Anonymous")
    slot_id = data.get("slot_id")
    code = data.get("code")
    if slot_id is None or code is None:
        return jsonify({"status": "error", "message": "Missing slot_id or code"}), 400
    try:
        raw_slot_id = int(slot_id)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid slot_id"}), 400
    with G_STATE.vision_lock:
        if 0 <= raw_slot_id < len(G_STATE.slots):
            idx = raw_slot_id
        elif 1 <= raw_slot_id <= len(G_STATE.slots):
            idx = raw_slot_id - 1
        else:
            return jsonify({"status": "error", "message": "Slot not found"}), 404
        slot = G_STATE.slots[idx]
        track_id = slot.locked_track_id if slot.locked_track_id is not None else -1
        booking = G_STATE.auth_engine.bookings.get(idx)
        if not booking:
            return jsonify({"status": "error", "message": "No booking found for slot"}), 409
        if booking.get("user") != username:
            return jsonify({"status": "error", "message": "Booking user mismatch"}), 403
        if booking.get("auth_code") != code and not DEV_MODE:
            return jsonify({"status": "error", "message": "Invalid auth code"}), 400
        auth_status, _ = G_STATE.auth_engine.authorize_vehicle(idx, code, track_id)
        if auth_status != "success":
            return jsonify({"status": "error", "message": f"Authorization failed: {auth_status}"}), 409
        is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
        if track_id is None or track_id < 0:
            return jsonify({"status": "error", "message": "Vehicle not detected in selected slot"}), 409
        if not is_aligned:
            return jsonify({"status": "error", "message": "Park correctly in the assigned slot before starting charge"}), 409
        slot.set_state(SlotState.AUTH_ACTIVE, track_id=track_id)
        if not slot.set_state(SlotState.CHARGING, track_id=track_id):
            return jsonify({"status": "error", "message": "Unable to start charging for this slot"}), 409
        G_STATE.auth_engine.consume_booking(idx)
        with G_STATE.session_lock:
            G_STATE.sessions[idx] = {
                "battery_pct": 20.0,
                "power": 7.2,
                "energy": 0.0,
                "start_time": utils.system_now(caller="api_thread")
            }
    logger.info(f"[CHARGING] Manual start success user={username} slot={idx+1}")
    return jsonify({"status": "success", "slot_id": idx + 1, "message": "Charging started"})

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
    logger.info("Server running")
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
        G_STATE.slots, G_STATE.queue_manager, G_STATE.camera_online = slots, queue_manager, False
    logger.info("State initialized")
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
                logger.warning("Camera unavailable (degraded mode)")
                cap = None
            else:
                logger.info("Camera initialized")
                with G_STATE.vision_lock:
                    G_STATE.camera_online = True
                cv2.namedWindow("Smart EV Charging")
        else:
            logger.info("Camera initialized")
            with G_STATE.vision_lock:
                G_STATE.camera_online = True
            cv2.namedWindow("Smart EV Charging")

    else:
        cap = None
        with G_STATE.vision_lock:
            G_STATE.camera_online = True
    
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
    detect_every_n = max(1, int(CONFIG.get('detect_every_n_frames', 2)))
    last_tracker_detections = sv.Detections.empty()
    last_detect_duration = 0.0
    ui_fps_ema = 0.0
    while True:
        frame_id += 1
        loop_start = utils.system_now(caller="main_loop")
        dt = loop_start - last_loop_time
        last_loop_time = loop_start
        if dt > 0:
            inst_fps = 1.0 / max(dt, 1e-6)
            ui_fps_ema = inst_fps if ui_fps_ema == 0.0 else (0.9 * ui_fps_ema + 0.1 * inst_fps)
        
        # 1. Dual-Condition Watchdog (Stagnation OR Compute Hang)
        is_replay = CONFIG.get("replay_mode", False)
        in_startup_grace = (loop_start - G_STATE.startup_time < 2.0)
        
        if not in_startup_grace and not is_replay:
            watchdog_timeout = 15.0 if DEV_MODE else 4.0
            if dt > watchdog_timeout:
                 trigger_freeze(f"SYSTEM_STALL: Compute hang detected (>{watchdog_timeout}s)")
        
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
            if cap is None:
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                detections = sv.Detections.empty()
                time.sleep(0.05)
                if frame_id % 60 == 0:
                    logger.info("[HEARTBEAT] Camera unavailable (degraded mode)")
            else:
                ret, frame = cap.read()
                if not ret:
                    logger.error("[CAMERA] Failed to grab frame. Reconnecting...")
                    cap.release()
                    time.sleep(1.0)
                    cap = cv2.VideoCapture(CONFIG.get('camera_index', 1), cv2.CAP_DSHOW)
                    if cap.isOpened():
                        with G_STATE.vision_lock:
                            G_STATE.camera_online = True
                        logger.info("Camera initialized")
                    else:
                        cap = None
                        with G_STATE.vision_lock:
                            G_STATE.camera_online = False
                        logger.warning("Camera unavailable (degraded mode)")
                    continue
                with G_STATE.vision_lock:
                    G_STATE.camera_online = True
                t_grab = time.time() - t0

                # 2. Detect (AI) with configurable frame skipping for responsiveness.
                do_detect = (frame_id % detect_every_n == 0) or len(last_tracker_detections) == 0
                if do_detect:
                    try:
                        t1 = time.time()
                        detections = tracker.update_with_detections(detector.detect(frame, conf=0.15))
                        t_detect = time.time() - t1
                        last_detect_duration = t_detect
                        last_tracker_detections = detections
                    except Exception as e:
                        logger.error(f"[VISION] Detection error: {e}")
                        detections = sv.Detections.empty()
                        t_detect = 0
                        last_detect_duration = t_detect
                        last_tracker_detections = detections
                else:
                    detections = last_tracker_detections
                    t_detect = last_detect_duration
                    
                if frame_id % 30 == 0:
                    logger.info(f"[PERF] Frame {frame_id}: Grab {t_grab:.3f}s | Detect {t_detect:.3f}s | FPS~{ui_fps_ema:.1f} | DetectEvery={detect_every_n}")
        
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
                                    with G_STATE.session_lock:
                                        if s_idx not in G_STATE.sessions:
                                            G_STATE.sessions[s_idx] = {
                                                "battery_pct": 20.0,
                                                "power": 7.2,
                                                "energy": 0.0,
                                                "start_time": utils.system_now(caller="main_loop")
                                            }
                                    logger.info(f"[AUTH] AUTH_ACTIVE -> CHARGING for Slot {s_idx+1}")
                                    logger.info(f"VALIDATED Session Start for Slot {s_idx+1}")

                        elif slot.state == SlotState.CHARGING:
                            is_auth, _ = G_STATE.auth_engine.is_authorized(s_idx, tid)
                            is_aligned = (slot.alignment_state == AlignmentState.ALIGNED and slot.smoothed_alignment_score >= ALIGN_THRESHOLD)
                            if not is_auth or not is_aligned:
                                logger.warning(f"[AUTH] SESSION TERMINATED for Slot {s_idx+1}")
                                slot.set_state(SlotState.FREE)
                                with G_STATE.session_lock:
                                    G_STATE.sessions.pop(s_idx, None)
                        slot.handle_occlusion(False, current_time=loop_start)

            # --- DEPARTURE & GHOST RECOVERY ---
            for i, slot in enumerate(G_STATE.slots):
                # Ghost Charging check
                if slot.state == SlotState.CHARGING and slot.locked_track_id is None:
                    logger.critical(f"[INVARIANT] Ghost Charging on Slot {i+1} -> FORCED RESET")
                    slot.force_safe_state()
                    G_STATE.auth_engine.revoke_authorization(i)
                    with G_STATE.session_lock:
                        G_STATE.sessions.pop(i, None)

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
            else:
                degraded_snapshot = build_pipeline_placeholder_snapshot(
                    frame_id,
                    loop_start,
                    G_STATE.freeze_reason or "SNAPSHOT_PIPELINE_FAILURE"
                )
                G_STATE.snapshot_buffer.append(degraded_snapshot)
                logger.warning(
                    "[SNAPSHOT] Fallback placeholder emitted seq=%s reason=%s",
                    degraded_snapshot.get("snapshot_sequence"),
                    degraded_snapshot.get("mode_reason")
                )

                # 3. Periodic Referential Integrity Audit (Reuse current_snapshot)
                if current_snapshot and frame_id % CONFIG.get("integrity_interval", 30) == 0:
                    validate_referential_integrity(current_snapshot)
        else:
            # Auto-unfreeze safety (60s)
            if loop_start - G_STATE.freeze_start_time > 60.0:
                logger.warning("[FORENSICS] Auto-unfreezing buffer (Timeout)")
                G_STATE.is_forensic_frozen = False
            frozen_snapshot = build_pipeline_placeholder_snapshot(
                frame_id,
                loop_start,
                G_STATE.freeze_reason or "FORENSIC_FREEZE_ACTIVE"
            )
            G_STATE.snapshot_buffer.append(frozen_snapshot)

        if frame_id % 30 == 0:
            latest = G_STATE.snapshot_buffer[-1] if G_STATE.snapshot_buffer else {}
            logger.info(
                "[HEARTBEAT] frame=%d seq=%s mode=%s camera=%s slots=%d queue=%d",
                frame_id,
                latest.get("snapshot_sequence", -1),
                latest.get("system_mode", "UNKNOWN"),
                "ACTIVE" if G_STATE.camera_online else "DEGRADED",
                len(latest.get("slots", [])),
                len(latest.get("queue", []))
            )

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

        # --- OVERLAYS ---
        frame = visualizer.draw_overlays(frame, G_STATE.slots, CONFIG.get('queue_zones', []), {})
        frame = visualizer.draw_detections(frame, detections)
        frame = visualizer.draw_sidebar(frame, G_STATE.queue_manager)
        perf_lines = [
            f"FPS: {ui_fps_ema:.1f}",
            f"Detect: {last_detect_duration*1000:.0f} ms",
            f"Detect Every: {detect_every_n} frame(s)"
        ]
        cv2.rectangle(frame, (10, 10), (300, 86), (0, 0, 0), -1)
        for i, text in enumerate(perf_lines):
            cv2.putText(frame, text, (18, 32 + (i * 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 255, 120), 1, cv2.LINE_AA)

        if cap is not None:
            cv2.imshow("Smart EV Charging", frame)
            key = cv2.waitKey(1) & 0xFF
        else:
            key = 255
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
                logger.info("[CALIB] Entering Slot Selection Mode...")
                new_slots = select_zones(cap, "Select Charging Slots", current_zones=CONFIG.get('slots', []), mode="slot")
                if new_slots:
                    CONFIG['slots'] = new_slots
                    # Immediate Persistence
                    try:
                        with open("config.yaml", 'w') as f:
                            yaml.dump(CONFIG, f)
                        logger.info(f"[CONFIG] Saved {len(new_slots)} slots.")
                    except Exception as e:
                        logger.error(f"[CONFIG] Auto-save failed: {e}")
                    
                    # Live State Update
                    with G_STATE.vision_lock:
                        G_STATE.slots = [Slot(i, np.array(poly, np.int32)) for i, poly in enumerate(new_slots)]
                    logger.info(f"[CALIB] Live state synchronized with {len(new_slots)} slots.")

            elif key == ord('z'):
                logger.info("[CALIB] Entering Queue Zone Selection Mode...")
                new_zones = select_zones(cap, "Select Queue Zones", current_zones=CONFIG.get('queue_zones', []), mode="queue")
                if new_zones:
                    CONFIG['queue_zones'] = new_zones
                    try:
                        with open("config.yaml", 'w') as f:
                            yaml.dump(CONFIG, f)
                        logger.info(f"[CONFIG] Saved {len(new_zones)} queue zones.")
                    except Exception as e:
                        logger.error(f"[CONFIG] Auto-save failed: {e}")

            elif key == ord('c'):
                logger.info("[CALIB] Clearing all zones as per earlier reference behavior...")
                CONFIG['slots'] = []
                CONFIG['queue_zones'] = []
                try:
                    with open("config.yaml", 'w') as f:
                        yaml.dump(CONFIG, f)
                    logger.info("[CONFIG] Config cleared.")
                except Exception as e:
                    logger.error(f"[CONFIG] Clear-save failed: {e}")
                
                with G_STATE.vision_lock:
                    G_STATE.slots = []
                logger.info("[CALIB] Live state cleared.")

        else:
            # Already slept at the top of the loop in FAKE mode
            pass

    if cap: cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__": main()
