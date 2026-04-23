import requests
import time
import threading
import numpy as np

BASE_URL = "http://127.0.0.1:5001/api"

def test_rate_limiting():
    print("\n--- T-909: Global & Per-IP Rate Limiting ---")
    # Spam suggestions
    success_count = 0
    fail_count = 0
    for _ in range(110): # Capacity is 100
        r = requests.get(f"{BASE_URL}/suggestions")
        if r.status_code == 200:
            success_count += 1
        else:
            fail_count += 1
    
    print(f"Global Success: {success_count}, Fails: {fail_count}")
    assert fail_count > 0, "Global rate limit did not trigger"

    # Test priority bypass
    print("Testing /api/authorize bypass...")
    r = requests.post(f"{BASE_URL}/authorize", json={"slot_id": 0, "code": "ABC", "track_id": 1})
    # Even if global cap is hit, this might pass if per-IP isn't hit
    print(f"Authorize Response: {r.status_code}")
    # Note: Authorize might return 400 for bad data, but it shouldn't return 429 if bypass works

def test_ulid_integrity():
    print("\n--- T-906: ULID Integrity & Ordering ---")
    from src.industrial_utils import EVENT_BUS
    ids = []
    for _ in range(1000):
        ids.append(EVENT_BUS.next_id())
    
    # Check uniqueness
    assert len(set(ids)) == 1000, "Duplicate IDs found"
    
    # Check ordering
    sorted_ids = sorted(ids)
    assert ids == sorted_ids, "ULIDs are not monotonic"
    print("ULID Uniqueness & Ordering Verified.")

def test_forensics():
    print("\n--- T-918: Forensic Freeze & Snapshot ---")
    # 1. Check suggestions?debug=true
    r = requests.get(f"{BASE_URL}/suggestions?debug=true")
    data = r.json()
    print(f"Snapshot Keys: {list(data.keys())}")
    if "slots" in data and len(data["slots"]) > 0:
        print(f"Slot 1 Keys: {list(data['slots'][0].keys())}")
        if "industrial" in data["slots"][0]:
             print(f"Slot 1 Industrial: {data['slots'][0]['industrial']}")

    assert "intelligence" in data or "suggestions" in data, "Suggestions missing"
    # Note: intelligence only appears if queue_manager is active
    
    # 2. Test Freeze
    print("Freezing buffer...")
    requests.post(f"{BASE_URL}/forensics/freeze")
    r1 = requests.get(f"{BASE_URL}/forensics")
    f1 = r1.json()
    f1_len = len(f1)
    print(f"Initial Forensic Length: {f1_len}")
    
    time.sleep(2.0)
    r2 = requests.get(f"{BASE_URL}/forensics")
    f2_len = len(r2.json())
    print(f"Post-Freeze Forensic Length: {f2_len}")
    
    assert f1_len == f2_len, "Forensic buffer continued rolling during freeze"
    print("Forensic Freeze Verified.")

    # 3. Test Unfreeze
    print("Unfreezing buffer...")
    requests.post(f"{BASE_URL}/forensics/unfreeze")
    time.sleep(2.0)
    r3 = requests.get(f"{BASE_URL}/forensics")
    f3_len = len(r3.json())
    print(f"Post-Unfreeze Forensic Length: {f3_len}")
    assert f3_len > f2_len or f3_len == 100, "Forensic buffer failed to resume"
    print("Forensic Unfreeze Verified.")

if __name__ == "__main__":
    print("Starting Industrial Verification...")
    try:
        test_ulid_integrity()
        
        print("\nWaiting for rate limiters to stabilize...")
        time.sleep(5)
        
        test_rate_limiting()
        
        print("\nWaiting for rate limiters to refill...")
        time.sleep(11) # Full 100 token refill at 10/s
        
        test_forensics()
        print("\nALL INDUSTRIAL TESTS PASSED.")
    except Exception as e:
        print(f"Verification Failed: {e}")
        import traceback
        traceback.print_exc()
