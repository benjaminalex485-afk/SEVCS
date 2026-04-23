import requests
import time
import subprocess
import os
import json
import unittest
from sevcs_tests.test_harness import SEVCSTestHarness
from sevcs_tests.log_validator import LogValidator

BACKEND_URL = "http://127.0.0.1:5001"

class TestHardening(unittest.TestCase):
    def setUp(self):
        self.harness = SEVCSTestHarness(BACKEND_URL)
        self.validator = LogValidator()

    def test_01_suggestion_state_progression(self):
        """Verify monotonic SOFT -> STABLE -> COMMITTED progression."""
        # Run priority test to get a stable suggestion
        self.harness.start_backend("stage3_priority_test")
        
        # Wait for a suggestion to appear
        track_id = None
        for _ in range(20):
            r = requests.get(f"{BACKEND_URL}/api/suggestions")
            suggestions = r.json()
            active = [s for s in suggestions if s["track_id"] is not None]
            if active:
                track_id = active[0]["track_id"]
                break
            time.sleep(0.5)
        
        self.assertIsNotNone(track_id, "No suggestion appeared")
        
        # Check progression
        states_seen = set()
        start = time.time()
        while time.time() - start < 10.0:
            r = requests.get(f"{BACKEND_URL}/api/suggestions")
            suggestions = r.json()
            for s in suggestions:
                if s["track_id"] == track_id:
                    states_seen.add(s["state"])
            if "COMMITTED" in states_seen: break
            time.sleep(0.5)
        
        self.harness.stop_backend()
        self.assertIn("SOFT", states_seen)
        self.assertIn("STABLE", states_seen)
        self.assertIn("COMMITTED", states_seen)

    def test_02_transient_alignment_drop(self):
        """Verify system does NOT revoke on 1-frame alignment drop."""
        # 1. Start with transient drop scenario
        # Step 1: Book and Authorize
        # Step 2: Scenario drops alignment at t=15s for 0.1s
        # Step 3: Verify no 'REVOKE' in logs
        api_steps = [
            (2.0, "POST", "/api/book", {"username": "Tester"}),
            (10.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
        ]
        self.harness.run_scenario("stage3_5_transient_drop", api_steps)
        
        self.validator.validate_scenario("Transient Alignment Drop", 
            expected_patterns=[r"AUTH_ACTIVE -> CHARGING"],
            forbidden_patterns=[r"REVOKE: MISALIGNED"]
        )

    def test_03_occlusion_trust_continuity(self):
        """Verify trust (STABLE) survives 200ms occlusion."""
        self.harness.start_backend("stage3_5_occlusion_recovery")
        
        # 1. Wait for STABLE trust
        stable_reached = False
        start = time.time()
        while time.time() - start < 15.0:
            r = requests.get(f"{BACKEND_URL}/api/suggestions")
            suggestions = r.json()
            active = [s for s in suggestions if s["track_id"] is not None and s["state"] in ["STABLE", "COMMITTED"]]
            if active:
                stable_reached = True
                break
            time.sleep(0.5)
        
        self.assertTrue(stable_reached, "Trust never reached STABLE before occlusion")
        
        # 2. Wait for occlusion (t=5s in scenario, we started at t=0)
        # The scenario drops at t=5s. We wait until t=7s to ensure it's recovered.
        time.sleep(5.0)
        
        # 3. Verify trust is still STABLE/COMMITTED and not reset to SOFT
        r = requests.get(f"{BACKEND_URL}/api/suggestions")
        suggestions = r.json()
        active = [s for s in suggestions if s["track_id"] is not None]
        self.assertTrue(len(active) > 0, "Vehicle lost after occlusion")
        self.assertIn(active[0]["state"], ["STABLE", "COMMITTED"], "Trust reset to SOFT after occlusion")
        
        self.harness.stop_backend()

if __name__ == "__main__":
    unittest.main()
