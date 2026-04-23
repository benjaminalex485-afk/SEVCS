from enum import Enum
import time
import hashlib
import uuid
import collections
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

# Import numpy for percentile calculation if needed, otherwise use simple sorted logic
import numpy as np
