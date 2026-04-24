import cv2
import yaml
import numpy as np
import os
import time

_TIME_GUARD_ENABLED = True
_ALLOWED_CALLERS = {"main_loop", "watchdog_thread", "api_thread"}

def system_now(caller=None):
    """Authorized monotonic time access. Only for loop/watchdog."""
    if _TIME_GUARD_ENABLED and caller not in _ALLOWED_CALLERS:
        raise RuntimeError(f"Unauthorized time access from: {caller}. Use frame_time instead.")
    return time.time()

def now():
    """DEPRECATED: Use frame_time in logic or system_now('main_loop') in core."""
    raise RuntimeError("Use frame_time instead of now()")

class FrozenConfig(dict):
    """Immutable config wrapper to prevent runtime mutation."""
    def __setitem__(self, key, value):
        raise RuntimeError(f"Attempted to mutate frozen config: {key}")
    def update(self, *args, **kwargs):
        raise RuntimeError("Attempted to mutate frozen config via update()")

def freeze_config(config_dict):
    """Recursively freeze a dictionary."""
    if isinstance(config_dict, dict):
        return FrozenConfig({k: freeze_config(v) for k, v in config_dict.items()})
    elif isinstance(config_dict, list):
        return tuple(freeze_config(i) for i in config_dict)
    return config_dict

def load_config(config_path="config.yaml"):
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def validate_config(config):
    """Enforces safety invariants on the system configuration."""
    assert "queue_zones" in config, "Missing 'queue_zones' in config"
    for poly in config["queue_zones"]:
        assert len(poly) >= 3, "Queue zone polygon must have at least 3 points"
        for p in poly:
            assert len(p) == 2, f"Invalid point in queue zone: {p}"
    
    # Defaults for Stage 3.5 hardening
    config.setdefault("strict_mode", False)
    config.setdefault("debug_locks", False)
    config.setdefault("distance_normalization", 1500.0)
    config.setdefault("auth_timeout", 60.0)
    config.setdefault("booking_timeout", 600.0)
    return True

def save_config(data, path="config.yaml"):
    with open(path, "w") as f:
        yaml.dump(data, f)

def select_zones(cap, title="Selection Mode", current_zones=None, mode="slot"):
    """
    Opens an interactive window to define polygons.
    mode: "slot" (Green) or "queue" (Blue)
    """
    if current_zones is None:
        current_zones = []
    
    # Use existing cap to avoid Windows MSMF resource conflicts
    ret, frame = cap.read()
    if not ret:
        print("Error: Could not read from camera.")
        return current_zones
    
    temp_zones = current_zones.copy()
    current_polygon = []
    
    window_name = title
    cv2.namedWindow(window_name)

    def mouse_callback(event, x, y, flags, param):
        nonlocal current_polygon
        if event == cv2.EVENT_LBUTTONDOWN:
            current_polygon.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN:
            if len(current_polygon) > 2:
                temp_zones.append(current_polygon.copy())
                current_polygon = []

    cv2.setMouseCallback(window_name, mouse_callback)

    color_map = {"slot": (0, 255, 0), "queue": (255, 0, 0)} # Green, Blue
    draw_color = color_map.get(mode, (0, 255, 0))

    while True:
        display_frame = frame.copy()
        
        # Draw existing zones
        for zone in temp_zones:
            pts = np.array(zone, np.int32)
            pts = pts.reshape((-1, 1, 2))
            cv2.polylines(display_frame, [pts], isClosed=True, color=draw_color, thickness=2)

        # Draw current polygon in progress
        if len(current_polygon) > 0:
            pts = np.array(current_polygon, np.int32)
            pts = pts.reshape((-1, 1, 2))
            cv2.polylines(display_frame, [pts], isClosed=False, color=(0, 0, 255), thickness=1)
            # Draw points
            for pt in current_polygon:
                cv2.circle(display_frame, tuple(pt), 3, (0, 0, 255), -1)

        # Instructions overlay
        cv2.putText(display_frame, f"Mode: {mode.upper()}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, draw_color, 2)
        cv2.putText(display_frame, "L-Click: Add Point | R-Click: Close", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(display_frame, "'z': Undo | 'c': Clear | 'q': Save", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

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

def get_charging_input(slot_id):
    """
    Opens a simple dialog to get kWh and Rate.
    Returns: (kwh, rate) or (None, None) if cancelled.
    """
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw() # Hide main window

    kwh = simpledialog.askfloat(f"Slot {slot_id} Setup", "Enter Energy Needed (kWh):", minvalue=1.0, maxvalue=200.0)
    if kwh is None:
        root.destroy()
        return None, None
    
    rate = simpledialog.askfloat(f"Slot {slot_id} Setup", "Enter Charging Rate (kW):", minvalue=1.0, maxvalue=350.0)
    if rate is None:
        root.destroy()
        return None, None

    root.destroy()
    return kwh, rate


def serialize_detections(detections):
    if detections is None: return None
    return {
        'xyxy': detections.xyxy.tolist(),
        'confidence': detections.confidence.tolist() if detections.confidence is not None else None,
        'class_id': detections.class_id.tolist() if detections.class_id is not None else None,
        'tracker_id': detections.tracker_id.tolist() if detections.tracker_id is not None else None
    }

def deserialize_detections(data):
    if data is None: return None
    import supervision as sv
    import numpy as np
    return sv.Detections(
        xyxy=np.array(data['xyxy']),
        confidence=np.array(data['confidence']) if data['confidence'] is not None else None,
        class_id=np.array(data['class_id']) if data['class_id'] is not None else None,
        tracker_id=np.array(data['tracker_id']) if data['tracker_id'] is not None else None
    )

def normalize_float(x):
    """Rounds all floats to 6 decimals to prevent bit-drift across runs/machines."""
    if isinstance(x, (float, np.float32, np.float64)):
        return round(float(x), 6)
    return x

def is_finite_numeric(x):
    """Rejects NaN or Inf to prevent decision logic corruption."""
    if isinstance(x, (float, np.float32, np.float64)):
        import math
        return math.isfinite(x)
    return True

def remove_none_fields(obj):
    """Recursively strips None fields to ensure structural canonicalization."""
    if isinstance(obj, dict):
        return {
            k: remove_none_fields(v)
            for k, v in obj.items()
            if v is not None
        }
    elif isinstance(obj, list):
        return [remove_none_fields(v) for v in obj]
    else:
        return obj

def deep_sort(obj):
    """Recursively sorts all nested dictionaries for bit-perfect hashing."""
    if isinstance(obj, dict):
        return {k: deep_sort(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [deep_sort(x) for x in obj]
    return obj

def normalize_state(obj):
    """
    Applies the full 3-step normalization pipeline:
    1. Strip None fields (Canonical structure)
    2. Round floats (Numeric stability)
    3. Deep sort (Ordering stability)
    Result is bit-perfect idempotent state.
    """
    # 1. Clean structure
    obj = remove_none_fields(obj)
    
    # 2. Stable numbers
    def _recurse_float(o):
        if isinstance(o, dict):
            return {k: _recurse_float(v) for k, v in o.items()}
        elif isinstance(o, list):
            return [_recurse_float(v) for v in o]
        else:
            return normalize_float(o)
            
    obj = _recurse_float(obj)
    
    # 3. Stable ordering
    return deep_sort(obj)
