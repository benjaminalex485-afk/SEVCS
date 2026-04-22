import requests
import threading
import time

URL = "http://localhost:5001/api/book"

def send_request(thread_id):
    payload = {
        "username": f"StressUser_{thread_id}", 
        "kwh": 20, 
        "type": "Standard"
    }
    try:
        r = requests.post(URL, json=payload, timeout=5)
        print(f"[Thread {thread_id}] Status: {r.status_code} | Body: {r.json()}")
    except Exception as e:
        print(f"[Thread {thread_id}] Error: {e}")

def run_flood_test(num_threads=20):
    print(f"Starting API Flood Test with {num_threads} simultaneous requests...")
    threads = [threading.Thread(target=send_request, args=(i,)) for i in range(num_threads)]
    for t in threads: t.start()
    for t in threads: t.join()
    print("Flood test complete.")

if __name__ == "__main__":
    run_flood_test()
