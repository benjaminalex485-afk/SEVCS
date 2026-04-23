import re
import os

class LogValidator:
    def __init__(self, log_path="sevcs_events.log"):
        self.log_path = log_path
        self.logs = []
        self._load_logs()

    def _load_logs(self):
        if not os.path.exists(self.log_path):
            return
        with open(self.log_path, 'r') as f:
            self.logs = f.readlines()

    def find_event(self, pattern, start_line=0):
        for i in range(start_line, len(self.logs)):
            if re.search(pattern, self.logs[i]):
                return i
        return -1

    def verify_sequence(self, patterns):
        """
        patterns: list of regex patterns that must appear in order
        """
        current_line = 0
        for pattern in patterns:
            line_idx = self.find_event(pattern, current_line)
            if line_idx == -1:
                return False, f"Pattern not found: {pattern}"
            current_line = line_idx + 1
        return True, "All patterns found in order"

    def never_event(self, pattern):
        idx = self.find_event(pattern)
        if idx != -1:
            return False, f"Forbidden event found: {pattern} at line {idx+1}"
        return True, "Forbidden event not found"

    def validate_scenario(self, name, expected_patterns, forbidden_patterns=None):
        print(f"\n--- VALIDATING SCENARIO: {name} ---")
        self._load_logs() # RELOAD FRESH LOGS
        
        # Check sequence
        success, msg = self.verify_sequence(expected_patterns)
        if not success:
            print(f"FAILED: {msg}")
            return False
            
        # Check forbidden
        if forbidden_patterns:
            for pattern in forbidden_patterns:
                success, msg = self.never_event(pattern)
                if not success:
                    print(f"FAILED: {msg}")
                    return False
                    
        print("PASSED")
        return True
