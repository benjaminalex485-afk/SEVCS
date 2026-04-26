import cv2
import yaml
import numpy as np

def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)

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
