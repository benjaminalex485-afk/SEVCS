import requests
import time
import json

URL = "http://localhost:5001/api/status"

def run_snapshot_stress(duration_sec=10):
    print(f"Starting Snapshot Stress Test for {duration_sec}s...")
    start = time.time()
    count = 0
    inconsistencies = 0

    while time.time() - start < duration_sec:
        try:
            r = requests.get(URL, timeout=2)
            if r.status_code == 200:
                data = r.json()
                # Validation Logic: Session must only exist if state is CHARGING
                for slot in data.get("slots", []):
                    state = slot.get("state")
                    session = slot.get("session")
                    if state == "CHARGING" and not session:
                        print(f"!!! INCONSISTENCY: Slot {slot['id']} is CHARGING but has no session data.")
                        inconsistencies += 1
                count += 1
            else:
                print(f"Error: Status {r.status_code}")
        except Exception as e:
            print(f"Request Error: {e}")
        
    print(f"--- Results ---")
    print(f"Total Polls: {count}")
    print(f"Frequency: {count/duration_sec:.1f} Hz")
    print(f"Inconsistencies: {inconsistencies}")
    
if __name__ == "__main__":
    run_snapshot_stress()
