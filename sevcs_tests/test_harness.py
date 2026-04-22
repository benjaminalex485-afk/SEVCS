import subprocess
import time
import requests
import os
import signal
import json
import logging
from sevcs_tests.log_validator import LogValidator

logging.basicConfig(level=logging.INFO, format='%(asctime)s [HARNESS] %(message)s')
logger = logging.getLogger(__name__)

class SEVCSTestHarness:
    def __init__(self, api_url="http://localhost:5001"):
        self.api_url = api_url
        self.process = None
        self.log_file = "sevcs_events.log"

    def start_backend(self, scenario):
        logger.info(f"Starting SEVCS with scenario: {scenario}")
        # Clear logs safely
        for _ in range(5):
            try:
                if os.path.exists(self.log_file):
                    os.remove(self.log_file)
                break
            except:
                time.sleep(1)
            
        env = os.environ.copy()
        env["SEVCS_SCENARIO"] = scenario
        python_exe = r"C:\Users\benjamin.ka\AppData\Local\Programs\Python\Python312\python.exe"
        # Running main.py as a subprocess
        self.process = subprocess.Popen([python_exe, "main.py"], env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        
        # Wait for API to be ready
        for _ in range(20):
            try:
                requests.get(f"{self.api_url}/api/status")
                logger.info("Backend is ONLINE")
                return True
            except:
                time.sleep(0.5)
        logger.error("Backend failed to start")
        return False

    def stop_backend(self):
        if self.process:
            logger.info("Stopping backend...")
            if os.name == 'nt':
                subprocess.call(['taskkill', '/F', '/T', '/PID', str(self.process.pid)])
                try: self.process.wait(timeout=5)
                except: pass
            else:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                try: self.process.wait(timeout=5)
                except: pass
            self.process = None
            time.sleep(2)

    def run_scenario(self, scenario_name, api_steps):
        if not self.start_backend(scenario_name):
            return False
            
        start_time = time.monotonic()
        context = {} # Store response values
        
        for i, (delay, method, endpoint, data) in enumerate(api_steps):
            logger.info(f"--- EXECUTING STEP {i+1}/{len(api_steps)} ---")
            while time.monotonic() - start_time < delay:
                time.sleep(0.1)
            
            # Interpolate context into data and endpoint
            str_data = json.dumps(data)
            for k, v in context.items():
                str_data = str_data.replace(f"{{{{{k}}}}}", str(v))
                endpoint = endpoint.replace(f"{{{{{k}}}}}", str(v))
            
            logger.info(f"Step: {method} {endpoint} data={str_data}")
            url = f"{self.api_url}{endpoint}"
            try:
                if method == "POST":
                    resp = requests.post(url, json=json.loads(str_data))
                else:
                    resp = requests.get(url)
                
                logger.info(f"Response: {resp.status_code} {resp.text}")
                # Save response to context
                if resp.status_code == 200:
                    try:
                        context.update(resp.json())
                    except: pass
            except Exception as e:
                logger.error(f"API Call failed: {e}")
        
        # Wait for vision to catch up and state to stabilize (Increase for CHARGING transition)
        time.sleep(35)
        self.stop_backend()
        return True

if __name__ == "__main__":
    # Example usage (can be expanded into a test suite)
    harness = SEVCSTestHarness()
    
    # Happy Path Test
    harness.run_scenario("stage2_happy_path", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (16.0, "POST", "/api/authorize", {"slot_id": 1, "code": "654321"}) # Code will be dummy in this mock run
    ])
    
    validator = LogValidator("sevcs_events.log")
    # results = validator.validate_happy_path()
    # logger.info(f"Validation Results: {results}")
