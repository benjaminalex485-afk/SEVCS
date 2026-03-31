import os
import time
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder='data')

# System State
state = {
    "state": "IDLE", # IDLE, READY, CHARGING, FAULT, COMPLETE
    "voltage": 230.0,
    "current": 0.0,
    "power": 0.0,
    "energy": 0.0,
    "battery_pct": 20,
    "fault_type": "NONE",
    "uptime": 0
}

config = {
    "current_limit": 16.0,
    "tariff": 0.15
}

start_time = time.time()
session_start = 0

@app.route('/')
def index():
    return send_from_directory('data', 'index.html')

@app.route('/<path:path>')
def static_proxy(path):
    if os.path.exists(os.path.join('data', path)):
        return send_from_directory('data', path)
    return send_from_directory('data', 'index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    user = data.get('username')
    pw = data.get('password')
    for u in users_db:
        if u["username"] == user and u["password"] == pw:
             return jsonify({"status": "success", "role": u["role"]})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({"status": "success"})

@app.route('/api/status', methods=['GET'])
def get_status():
    state['uptime'] = int(time.time() - start_time)
    
    # Simulate charging logic
    if state['state'] == 'CHARGING':
        elapsed = time.time() - session_start
        state['current'] = config['current_limit']
        # Taper current if battery is near full
        if state['battery_pct'] > 90:
            state['current'] = max(2.0, config['current_limit'] * (100 - state['battery_pct']) / 10.0)
            
        state['power'] = state['voltage'] * state['current']
        # 1 kW = 1000W; 1 second = 1/3600 hour
        # Very fast simulation: 1 real second = many hours of charge
        state['energy'] += (state['power'] / 1000.0) * (50.0 / 3600.0)
        state['battery_pct'] = min(100, int(20 + elapsed * 1.5))
        
        if state['battery_pct'] >= 100:
            state['state'] = 'COMPLETE'
            state['current'] = 0.0
            state['power'] = 0.0
            
    return jsonify(state)

@app.route('/api/start', methods=['POST'])
def start_charging():
    if state['state'] in ['IDLE', 'READY', 'COMPLETE']:
        state['state'] = 'CHARGING'
        state['energy'] = 0.0
        state['battery_pct'] = max(20, state['battery_pct'] if state['state'] == 'COMPLETE' else 20)
        if state['battery_pct'] == 100:
             state['battery_pct'] = 20 # Reset for simulation
        global session_start
        session_start = time.time()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Cannot start from current state"}), 400

@app.route('/api/stop', methods=['POST'])
def stop_charging():
    state['state'] = 'READY'
    state['current'] = 0.0
    state['power'] = 0.0
    return jsonify({"status": "success"})

@app.route('/api/set_current', methods=['POST'])
def set_current():
    data = request.json or {}
    limit = data.get('limit')
    if limit is not None:
        config['current_limit'] = float(limit)
        return jsonify({"status": "success", "limit": config['current_limit']})
    return jsonify({"status": "error"}), 400

@app.route('/api/reset_fault', methods=['POST'])
def reset_fault():
    state['state'] = 'IDLE'
    state['fault_type'] = 'NONE'
    return jsonify({"status": "success"})

@app.route('/api/logs', methods=['GET'])
def get_logs():
    return jsonify([
        {"user": "user", "start_time": "12:00", "end_time": "12:45", "energy_kwh": 12.5, "cost": 1.87, "fault": "NONE"},
        {"user": "admin", "start_time": "13:00", "end_time": "13:10", "energy_kwh": 3.0, "cost": 0.45, "fault": "OVERCURRENT"}
    ])

camera_state = {
    "online": True,
    "state": "idle"
}

users_db = [
    {"username": "admin", "password": "admin", "role": "admin"},
    {"username": "user", "password": "user", "role": "user"}
]

@app.route('/api/camera/status', methods=['GET'])
def get_camera_status():
    return jsonify(camera_state)

@app.route('/api/camera/start', methods=['POST'])
def start_camera():
    camera_state["state"] = "active"
    return jsonify({"status": "success"})

@app.route('/api/camera/stop', methods=['POST'])
def stop_camera():
    camera_state["state"] = "idle"
    return jsonify({"status": "success"})

@app.route('/api/station/summary', methods=['GET'])
def get_station_summary():
    return jsonify({
        "charging": 1 if state['state'] == 'CHARGING' else 0,
        "reserved": len(bookings_db),
        "queue": 2
    })

@app.route('/api/slots', methods=['GET'])
def get_slots():
    return jsonify([
        {"id": 1, "state": "CHARGING" if state['state'] == 'CHARGING' else "FREE", "reserved_for": None, "estimate": time.time() + 3600},
        {"id": 2, "state": "FREE", "reserved_for": None, "estimate": None},
        {"id": 3, "state": "RESERVED", "reserved_for": "Pre-booked User", "estimate": None}
    ])

bookings_db = []

@app.route('/api/book', methods=['POST'])
def book_slot_mock():
    data = request.json or {}
    un = data.get('username', 'User')
    bookings_db.append(un)
    return jsonify({
        "status": "success",
        "slot_id": 2,
        "estimate": time.time() + 1800
    })

@app.route('/api/users', methods=['GET'])
def get_users_end():
    return jsonify({"users": [{"username": u["username"], "role": u["role"]} for u in users_db]})

@app.route('/api/users', methods=['POST'])
def post_user():
    data = request.json or {}
    un = data.get('username')
    pw = data.get('password')
    rl = data.get('role', 'user')
    if un and pw and len(users_db) < 8:
        for u in users_db:
             if u["username"] == un:
                 return jsonify({"status": "error", "message": "User exists"}), 400
        users_db.append({"username": un, "password": pw, "role": rl})
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/api/users', methods=['DELETE'])
def del_user():
    data = request.json or {}
    un = data.get('username')
    if un and un != 'admin':
        for i, u in enumerate(users_db):
            if u["username"] == un:
                del users_db[i]
                return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

if __name__ == '__main__':
    print("Starting mock backend on http://localhost:5000")
    if not os.path.exists('data'):
        os.makedirs('data')
    app.run(debug=True, port=5000, host='0.0.0.0')
