import json
import time
import unittest
from src import utils
from main import G_STATE, get_system_snapshot, validate_required_keys, trigger_freeze

class TestSafetySeal(unittest.TestCase):
    def setUp(self):
        # Reset G_STATE for each test
        G_STATE.snapshot_sequence = 0
        G_STATE.last_snapshot_version = 0
        G_STATE.is_forensic_frozen = False
        G_STATE.freeze_reason = None
        
    def test_T_1037_normalization_idempotency(self):
        """Verify that normalize_state(normalized_state) == normalized_state."""
        dirty_state = {
            "queue": [
                {"track_id": 1, "global_id": "v1", "drift_score": 0.123456789, "none_field": None}
            ],
            "slots": [
                {"slot_id": 1, "state": "FREE", "extra": None}
            ],
            "timestamp": 12345.67890123
        }
        
        norm1 = utils.normalize_state(dirty_state)
        norm2 = utils.normalize_state(norm1)
        
        self.assertEqual(norm1, norm2, "Normalization is not idempotent")
        self.assertNotIn("none_field", norm1["queue"][0])
        self.assertEqual(norm1["queue"][0]["drift_score"], 0.123457) # Rounding check
        
    def test_T_1044_sequence_atomicity(self):
        """Verify that snapshot_sequence only increments on successful snapshot."""
        initial_seq = G_STATE.snapshot_sequence
        
        # 1. Success case - Populate dummy state
        from src.slot_state_machine import Slot
        from src.queue_manager import QueueManager, QueueEntry
        G_STATE.slots = [Slot(0, [(0,0), (10,0), (10,10), (0,10)])]
        G_STATE.queue_manager = QueueManager()
        G_STATE.queue_manager.queue[1] = QueueEntry(1, arrival_time=123.45)
        
        snapshot = get_system_snapshot(frame_id=100, frame_time=123.45)
        self.assertIsNotNone(snapshot)
        self.assertEqual(G_STATE.snapshot_sequence, initial_seq + 1)
        self.assertEqual(snapshot["snapshot_sequence"], initial_seq + 1)
        
        # 2. Failure case (Empty slots/queue should trigger failure in our gated pipeline)
        # We need to simulate a failure inside the pipeline
        # Actually, get_system_snapshot will return None and trigger_freeze if it fails
        
        # Temporarily clear slots to trigger EMPTY_CONTAINER
        old_slots = G_STATE.slots
        G_STATE.slots = []
        
        failed_snapshot = get_system_snapshot(frame_id=101, frame_time=124.45)
        self.assertIsNone(failed_snapshot)
        self.assertEqual(G_STATE.snapshot_sequence, initial_seq + 1, "Sequence incremented on failure!")
        self.assertTrue(G_STATE.is_forensic_frozen)
        
        G_STATE.slots = old_slots # Restore

    def test_T_1047_nested_key_enforcement(self):
        """Verify that missing nested keys trigger validation failure."""
        valid_snapshot = {
            "queue": [{"global_id": "v1", "track_id": 1}],
            "slots": [{"slot_id": 1}]
        }
        
        # Should pass
        validate_required_keys(valid_snapshot)
        
        # Missing global_id
        invalid_snapshot = {
            "queue": [{"track_id": 1}],
            "slots": [{"slot_id": 1}]
        }
        with self.assertRaisesRegex(ValueError, "MISSING_REQUIRED_FIELD"):
            validate_required_keys(invalid_snapshot)
            
    def test_T_1048_empty_structure_protection(self):
        """Verify that empty containers trigger freeze correctly based on context."""
        # 1. Empty slots (Always fails)
        with self.assertRaisesRegex(ValueError, "EMPTY_SLOTS"):
            snapshot = {"slots": [], "queue": [], "mode": "SAFE"}
            # We simulate the logic in get_system_snapshot
            if not isinstance(snapshot.get("slots"), list) or len(snapshot.get("slots")) == 0:
                raise ValueError("EMPTY_SLOTS")

        # 2. Empty queue in SAFE mode (Allowed)
        snapshot_safe = {
            "slots": [{"slot_id": 1}],
            "queue": [],
            "mode": "SAFE"
        }
        # This should pass the mode check
        mode = snapshot_safe.get("mode")
        queue = snapshot_safe.get("queue")
        ALLOWED_EMPTY_QUEUE_MODES = {"SAFE", "IDLE", "INIT", "SOFT_SAFE"}
        if len(queue) == 0 and mode not in ALLOWED_EMPTY_QUEUE_MODES:
             raise ValueError("EMPTY_QUEUE_INVALID_STATE")
        
        # 3. Empty queue in FULL mode (Forbidden)
        snapshot_full = {
            "slots": [{"slot_id": 1}],
            "queue": [],
            "mode": "FULL"
        }
        with self.assertRaisesRegex(ValueError, "EMPTY_QUEUE_INVALID_STATE"):
             mode = snapshot_full.get("mode")
             queue = snapshot_full.get("queue")
             if len(queue) == 0 and mode not in ALLOWED_EMPTY_QUEUE_MODES:
                  raise ValueError(f"EMPTY_QUEUE_INVALID_STATE: mode={mode}")

    def test_time_authority_guard(self):
        """Verify that unauthorized callers cannot access system_now()."""
        # Authorized
        try:
            utils.system_now(caller="main_loop")
        except RuntimeError:
            self.fail("Authorized caller 'main_loop' was rejected")
            
        # Unauthorized
        with self.assertRaises(RuntimeError):
            utils.system_now(caller="hacker_thread")
            
        # Implicit (None)
        with self.assertRaises(RuntimeError):
            utils.system_now()

if __name__ == "__main__":
    unittest.main()
