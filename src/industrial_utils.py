from enum import Enum
import time
import hashlib
import uuid
import collections
import numpy as np
from src import utils

class ReasonCode(Enum):
    NONE = 0
    HOLD_TIMEOUT = 1
    THRASH = 2
    DRIFT = 3
    NO_VALID_CANDIDATE = 4
    TIME_ANOMALY = 5
    SYSTEM_STALL = 6
    INVARIANT_VIOLATION = 7
    LOW_CONFIDENCE = 8
    GEOMETRY_FALLBACK = 9
    STABLE_RECOVERY = 10

class SystemMode(Enum):
    FULL = 1
    SOFT_SAFE = 2
    SAFE = 3
    MINIMAL = 4

# --- STAGE 4.5: REALITY HARDENING ---
MIN_MOVEMENT = 5.0 # Min pixels moved to compute direction
DRIFT_THRESHOLD = 1.0 # Seconds before drift is flagged
SIGNAL_SMOOTHING = 0.3 # EWMA alpha

class SignalQuality:
    @staticmethod
    def compute_stability(history):
        """Returns 0-1 score based on tracking duration."""
        if not history: return 0.0
        return min(1.0, len(history) / 20.0)

    @staticmethod
    def compute_consistency(history):
        """Returns 0-1 score based on bbox area variance."""
        if len(history) < 5: return 1.0
        areas = []
        for _, _, bbox in history:
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            areas.append(area)
        
        mean_area = np.mean(areas)
        if mean_area == 0: return 0.0
        std_area = np.std(areas)
        # Higher variance = lower consistency
        return max(0.0, 1.0 - (std_area / mean_area))

    @staticmethod
    def normalize_safe(vec):
        """Safely normalize a vector with epsilon."""
        norm = np.linalg.norm(vec)
        if norm < 1e-6: return np.zeros_like(vec)
        return vec / (norm + 1e-6)

class EventGenerator:
    """Monotonic, sortable, sub-nanosecond event IDs."""
    def __init__(self):
        self.last_ts = 0
        self.counter = 0
        self.MAX_COUNTER = 0xFFFFFFFF # 32-bit wrap guard

    def next_id(self):
        now_ns = time.time_ns()
        if now_ns <= self.last_ts:
            self.counter += 1
            if self.counter > self.MAX_COUNTER:
                # Extreme overflow: bump timestamp
                now_ns = self.last_ts + 1
                self.counter = 0
        else:
            self.counter = 0
        
        self.last_ts = now_ns
        # Sortable ID: timestamp_ns + counter hex
        return f"{now_ns:019d}-{self.counter:08x}"

# Global Event Generator
EVENT_BUS = EventGenerator()

class IndustrialMetrics:
    """Bounded rolling-window metrics for industrial monitoring."""
    def __init__(self, window_size=300): # 10s at 30fps
        self.latencies = collections.deque(maxlen=window_size)
        self.thrash_history = collections.deque(maxlen=window_size)
        self.snapshot_id = 0
        self.MAX_SNAPSHOT_ID = 2**63 - 1

    def record_latency(self, val):
        self.latencies.append(val)

    def get_latency_p95(self):
        if not self.latencies: return 0.0
        return float(np.percentile(list(self.latencies), 95))

    def next_snapshot_id(self):
        self.snapshot_id = (self.snapshot_id + 1) % self.MAX_SNAPSHOT_ID
        return self.snapshot_id

