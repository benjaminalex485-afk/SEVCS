import time
import logging
import re
from sevcs_tests.test_harness import SEVCSTestHarness
from sevcs_tests.log_validator import LogValidator

def run_suite():
    harness = SEVCSTestHarness()
    validator = LogValidator()

    # --- T-101: HAPPY PATH ---
    harness.run_scenario("stage2_happy_path", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (16.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-101: Happy Path", [
        r"ALIGNMENT_PENDING -> AUTH_PENDING",
        r"AUTH_PENDING -> AUTH_ACTIVE",
        r"AUTH_ACTIVE -> CHARGING",
        r"VALIDATED Slot 1"
    ])

    # --- T-303: GHOST HUNTER ---
    harness.run_scenario("stage2_ghost", [])
    # Ghost is at (900, 400) which is OUTSIDE all slots. 
    validator.validate_scenario("T-303: Ghost Hunter", [], forbidden_patterns=[r"ALIGNMENT_PENDING", r"AUTH_PENDING"])

    # --- T-202: IDENTITY THEFT RACE ---
    # Using stage2_id_shift where Track 1 is replaced by Track 2
    harness.run_scenario("stage2_id_shift", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (22.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-202: Identity Theft Race", [
        r"AUTH_PENDING",
        r"REVOKE: ID_MISMATCH"
    ])

    # --- T-404: EXPIRY DURING AUTH ---
    harness.run_scenario("stage2_expiry", [
        (2.0, "POST", "/api/book", {"username": "Tester", "timeout": 10}),
        (40.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-404: Expiry during Auth", [
        r"AUTH_PENDING",
        r"REVOKE: EXPIRED"
    ])

    # --- T-508: QUEUE CLEANUP ---
    harness.run_scenario("stage3_cleanup_test", [])
    validator.validate_scenario("T-508: Queue Cleanup", [
        r"\[QUEUE\] ADD Track 1",
        r"\[QUEUE\] REMOVE Track 1 \(Stale\)"
    ])

    # --- T-515: EQUAL SCORE STABILITY ---
    harness.run_scenario("stage3_equal_score", [])
    validator.validate_scenario("T-515: Equal Score Stability (Slot 1)", [r"\[SUGGEST\] Slot .* -> Track 1"])
    validator.validate_scenario("T-515: Equal Score Stability (Slot 2)", [r"\[SUGGEST\] Slot .* -> Track 2"])

    # --- T-512: PRIORITY & BOOKING ---
    harness.run_scenario("stage3_priority_test", [])
    validator.validate_scenario("T-512: Priority Booking (Veh 2)", [r"\[SUGGEST\] Slot .* -> Track 2"])
    validator.validate_scenario("T-512: Priority Walk-in (Veh 1)", [r"\[SUGGEST\] Slot .* -> Track 1"])

if __name__ == "__main__":
    print("=== SEVCS PRODUCTION HARDENING VALIDATION SUITE ===")
    run_suite()
    print("=== VALIDATION COMPLETE ===")
