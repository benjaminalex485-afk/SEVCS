from flask import Flask, jsonify, request
from flask_cors import CORS
import time
import random
from functools import wraps

app = Flask(__name__)

# --- CONFIGURATION ---
PORT = 5001
ALLOWED_ORIGIN = "http://localhost:5500"

# Strict CORS Hardening
CORS(app, resources={
    r"/api/*": {
        "origins": [ALLOWED_ORIGIN],
        "methods": ["GET", "POST"],
        "allow_headers": ["Content-Type"]
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

import json
import hashlib

# --- ENDPOINTS ---
@app.route('/api/status', methods=['GET'])
@validate_contract
def get_status():
    G_STATE.tick()
    timestamp = int(time.time() * 1000)
    
    # 1. Construct Canonical Dictionary for Hashing (Strict Identity)
    canonical = {
        "sequence": G_STATE.sequence,
        "version": G_STATE.version,
        "timestamp": timestamp,
        "mode": G_STATE.mode,
        "health": G_STATE.health,
        "slots": G_STATE.slots,
        "queue": G_STATE.queue
    }
    
    # 2. Compute Identity Hash
    hash_input = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    state_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    # 3. Construct Full Contract Payload
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
        "state_hash": state_hash
    }
    
    return jsonify(payload)

@app.route('/api/authorize', methods=['POST'])
def authorize():
    data = request.json
    # Simulate processing
    return jsonify({
        "status": "OK",
        "snapshot_version": G_STATE.version,
        "replayed": False
    })

if __name__ == '__main__':
    print(f"--- SEVCS MOCK BACKEND STARTING ---")
    print(f"Origin Allowlist: {ALLOWED_ORIGIN}")
    print(f"Listening on: http://localhost:{PORT}")
    app.run(port=PORT, debug=True)
