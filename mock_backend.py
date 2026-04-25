from flask import Flask, jsonify, request
from flask_cors import CORS
import time
import random
from functools import wraps

app = Flask(__name__)

# --- CONFIGURATION ---
PORT = 5001
ALLOWED_ORIGIN = "*" 

# Strict CORS Hardening
CORS(app, resources={
    r"/api/*": {
        "origins": [ALLOWED_ORIGIN],
        "methods": ["GET", "POST"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# --- STATE ENGINE ---
class MockSystemState:
    def __init__(self):
        self.sequence = 1000
        self.version = 5000
        self.mode = "ACTIVE"
        self.health = 100
        self.freeze_state = False
        self.slots = [
            {"slot_id": 1, "state": "CHARGING", "assigned_global_id": 101},
            {"slot_id": 2, "state": "FREE", "assigned_global_id": None},
            {"slot_id": 3, "state": "RESERVED", "assigned_global_id": 102}
        ]
        self.queue = [
            {"global_id": 105, "track_id": 55, "state": "WAITING", "confidence": 0.98}
        ]
        self.users = {
            "test@example.com": {
                "id": 123,
                "name": "Test User",
                "password": "password",
                "role": "USER",
                "wallet": {"balance": 100.0, "currency": "USD"}
            },
            "admin@sevcs.com": {
                "id": 1,
                "name": "System Admin",
                "password": "admin",
                "role": "ADMIN",
                "wallet": {"balance": 0.0, "currency": "USD"}
            }
        }
        self.sessions = {} # token -> user_id
        
        # Stress Simulation Flags
        self.stress_mode = None # "JUMP" | "TIMEOUT" | "FREEZE_RACE"
        self.freeze_requested = False

    def tick(self):
        self.sequence += 1
        self.version += 1
        # Randomly fluctuate health
        self.health = max(90, min(100, self.health + random.randint(-1, 1)))

G_STATE = MockSystemState()

# --- CONTRACT VALIDATION LAYER ---
def validate_snapshot(data):
    """Semantic Validation per Requirements"""
    assert data["snapshot_sequence"] >= 0, "Negative sequence"
    assert data["snapshot_version"] >= 0, "Negative version"
    assert isinstance(data["slots"], list), "Slots must be a list"
    assert isinstance(data["queue"], list), "Queue must be a list"
    # CRITICAL: version must be >= sequence to prevent drift bugs
    assert data["snapshot_version"] >= data["snapshot_sequence"], "Version < Sequence drift detected"
    return True

def validate_contract(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = f(*args, **kwargs)
        data = response.get_json()
        
        required_fields = [
            "snapshot_sequence", "snapshot_version", 
            "system_mode", "system_health", 
            "slots", "queue", "freeze_state", "timestamp"
        ]
        
        missing = [field for field in required_fields if field not in data]
        if missing:
            return jsonify({
                "status": "ERROR",
                "code": "CONTRACT_VIOLATION",
                "reason": f"Missing required fields: {', '.join(missing)}"
            }), 500
            
        try:
            validate_snapshot(data)
        except AssertionError as e:
            return jsonify({
                "status": "ERROR",
                "code": "SEMANTIC_VIOLATION",
                "reason": str(e)
            }), 500
            
        return response
    return decorated_function

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"status": "ERROR", "message": "Unauthorized"}), 401
        
        token = auth_header.split(" ")[1]
        if token not in G_STATE.sessions:
            return jsonify({"status": "ERROR", "message": "Invalid or expired token"}), 401
        
        return f(*args, **kwargs)
    return decorated

import json
import hashlib

# --- AUTH ENDPOINTS ---
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    email = data.get("email")
    if email in G_STATE.users:
        return jsonify({"success": False, "message": "User already exists"}), 400
    
    user_id = random.randint(1000, 9999)
    G_STATE.users[email] = {
        "id": user_id,
        "name": data.get("name"),
        "password": data.get("password"),
        "role": "USER",
        "wallet": {"balance": 100.0, "currency": "USD"}
    }
    return jsonify({"success": True, "user_id": user_id})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")
    
    user = G_STATE.users.get(email)
    if not user or user["password"] != password:
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    
    token = hashlib.sha256(f"{email}{time.time()}".encode()).hexdigest()
    G_STATE.sessions[token] = user["id"]
    return jsonify({
        "success": True, 
        "token": token, 
        "user_id": user["id"],
        "role": user["role"]
    })

# --- SYSTEM ENDPOINTS ---
@app.route('/api/status', methods=['GET'])
@require_auth
@validate_contract
def get_status():
    token = request.headers.get("Authorization").split(" ")[1]
    user_id = G_STATE.sessions[token]
    user = next((u for u in G_STATE.users.values() if u["id"] == user_id), None)

    if G_STATE.stress_mode == "TIMEOUT":
        time.sleep(6) # Trigger client timeout
        G_STATE.stress_mode = None

    G_STATE.tick()
    
    if G_STATE.stress_mode == "JUMP":
        G_STATE.sequence += 50
        G_STATE.stress_mode = None

    timestamp = int(time.time() * 1000)
    
    payload = {
        "snapshot_sequence": G_STATE.sequence,
        "snapshot_version": G_STATE.version,
        "system_mode": G_STATE.mode,
        "system_health": G_STATE.health,
        "slots": G_STATE.slots,
        "queue": G_STATE.queue,
        "freeze_state": G_STATE.freeze_state,
        "timestamp": timestamp,
        "source": "BACKEND",
        "user_id": user_id,
        "user_wallet": user["wallet"] if user else None
    }
    
    return jsonify(payload)

@app.route('/api/book_slot', methods=['POST'])
@require_auth
def book_slot():
    data = request.json
    slot_id = int(data.get("payload", {}).get("slot_id"))
    
    if G_STATE.freeze_requested:
        G_STATE.freeze_state = True
        G_STATE.freeze_requested = False

    # Simulate processing delay
    time.sleep(0.5)

    for slot in G_STATE.slots:
        if slot["slot_id"] == slot_id:
            if slot["state"] == "FREE":
                slot["state"] = "RESERVED"
                slot["assigned_global_id"] = 123 # Mock ID
                return jsonify({
                    "status": "OK",
                    "snapshot_version": G_STATE.version,
                    "snapshot_sequence": G_STATE.sequence,
                    "replayed": False
                })
            return jsonify({"status": "REJECTED", "error": {"reason": "Slot already occupied"}})
    
    return jsonify({"status": "REJECTED", "error": {"reason": "Invalid slot ID"}})

@app.route('/api/recharge', methods=['POST'])
@require_auth
def recharge():
    token = request.headers.get("Authorization").split(" ")[1]
    user_id = G_STATE.sessions[token]
    user = next((u for u in G_STATE.users.values() if u["id"] == user_id), None)
    
    amount = request.json.get("payload", {}).get("amount", 0)
    if user:
        time.sleep(1) # Intent visibility test
        user["wallet"]["balance"] += amount
        return jsonify({
            "status": "OK", 
            "snapshot_version": G_STATE.version,
            "snapshot_sequence": G_STATE.sequence
        })
    
    return jsonify({"status": "REJECTED", "message": "User not found"}), 404

# --- STRESS CONTROL ---
@app.route('/api/debug/stress', methods=['POST'])
def set_stress():
    mode = request.json.get("mode")
    G_STATE.stress_mode = mode
    if mode == "FREEZE_RACE":
        G_STATE.freeze_requested = True
    return jsonify({"status": "OK", "active_mode": mode})

if __name__ == '__main__':
    print(f"--- SEVCS MOCK BACKEND STARTING ---")
    print(f"Origin Allowlist: {ALLOWED_ORIGIN}")
    print(f"Listening on: http://localhost:{PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=True)
