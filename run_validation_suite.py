from sevcs_tests.test_harness import SEVCSTestHarness
from sevcs_tests.log_validator import LogValidator
import time

def run_suite():
    harness = SEVCSTestHarness()
    validator = LogValidator()
    
    # --- T-101: HAPPY PATH + HYSTERESIS ---
    harness.run_scenario("stage2_happy_path", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (35.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-101: Happy Path", [
        r"ALIGNMENT_PENDING -> AUTH_PENDING",
        r"AUTH_PENDING -> AUTH_ACTIVE",
        r"AUTH_ACTIVE -> CHARGING",
        r"VALIDATED Slot 1"
    ])

    # --- T-303: GHOST HUNTER (Departure mid-Auth) ---
    # Scenario 'stage2_race' aligns then disappears at t=10
    harness.run_scenario("stage2_race", [
        (2.0, "POST", "/api/book", {"username": "Tester"})
    ])
    validator.validate_scenario("T-303: Ghost Hunter", [
        r"AUTH_PENDING",
        r"REVOKE: VEHICLE_LEFT",
        r"AUTH_PENDING -> FREE"
    ])

    # --- T-202: IDENTITY THEFT RACE ---
    # Scenario 'stage2_id_shift' aligns ID 1, then shifts to ID 2 at t=10
    harness.run_scenario("stage2_id_shift", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (35.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-202: Identity Theft Race", [
        r"AUTH_PENDING",
        r"REVOKE: ID_MISMATCH",
        r"ALIGNMENT_PENDING"
    ], forbidden_patterns=[r"AUTH_PENDING -> ACTIVE"])

    # --- T-404: EXPIRY MID-CHARGING ---
    # We use a short expiry for testing if we can, but here we'll just verify the REVOKE: EXPIRED logic
    harness.run_scenario("stage2_expiry", [
        (2.0, "POST", "/api/book", {"username": "Tester"}),
        (15.0, "POST", "/api/authorize", {"slot_id": 1, "code": "{{auth_code}}"})
    ])
    validator.validate_scenario("T-404: Expiry during Auth", [
        r"AUTH_PENDING",
        r"REVOKE: EXPIRED",
        r"AUTH_PENDING -> FREE"
    ])

if __name__ == "__main__":
    print("=== SEVCS PRODUCTION HARDENING VALIDATION SUITE ===")
    run_suite()
    print("=== VALIDATION COMPLETE ===")
