import time
from enum import Enum
import numpy as np
import logging
import cv2

# Configure logging
logger = logging.getLogger(__name__)

class SlotState(Enum):
    FREE = 0
    RESERVED = 1
    ALIGNMENT_PENDING = 2
    CHARGING = 3
    MISALIGNED = 4

class AlignmentState(Enum):
    UNSTABLE = 0
    STABILIZING = 1
    ALIGNED = 2
    MISALIGNED = 3

class Slot:
    def __init__(self, slot_id, polygon):
        self.slot_id = slot_id
        self.polygon = np.array(polygon, np.int32)
        self.bbox = cv2.boundingRect(self.polygon) # Precompute BBox for pre-filtering
        
        # State
        self.state = SlotState.FREE
        self.locked_track_id = None
        self.reservation_id = None # Store the ID of the vehicle that has reserved this slot
        self.track_age = 0 # Track age in frames for stability weighting
        self.safety_flag = False # True if safety issue detected
        
        # Alignment
        self.alignment_state = AlignmentState.UNSTABLE
        self.alignment_score = 0.0
        self.smoothed_alignment_score = 0.0
        
        # Timers
        self.last_evaluation_time = 0.0
        self.misalignment_timer = 0.0
        self.occlusion_timer = 0.0
        self.state_enter_time = time.time()

    def validate_transition(self, new_state):
        """
        Enforces physical reality by restricting allowed state jumps.
        """
        allowed = {
            SlotState.FREE: [SlotState.RESERVED, SlotState.ALIGNMENT_PENDING],
            SlotState.RESERVED: [SlotState.ALIGNMENT_PENDING, SlotState.FREE],
            SlotState.ALIGNMENT_PENDING: [SlotState.CHARGING, SlotState.MISALIGNED, SlotState.FREE],
            SlotState.CHARGING: [SlotState.MISALIGNED, SlotState.FREE],
            SlotState.MISALIGNED: [SlotState.CHARGING, SlotState.FREE]
        }
        return new_state in allowed.get(self.state, [])

    def set_state(self, new_state, track_id=None):
        if self.state != new_state:
            if not self.validate_transition(new_state):
                logger.error(f"[Slot {self.slot_id+1}] REJECTED Invalid Transition: {self.state.name} -> {new_state.name}")
                return False

            logger.info(f"[Slot {self.slot_id+1}] State Change: {self.state.name} -> {new_state.name} | Track: {track_id}")
            self.state = new_state
            self.state_enter_time = time.time()
            
            # Atomically update track ID during state transition
            if track_id is not None:
                self.locked_track_id = track_id
            
            if new_state == SlotState.FREE:
                self.locked_track_id = None
                self.alignment_state = AlignmentState.UNSTABLE
                self.smoothed_alignment_score = 0.0
                self.misalignment_timer = 0.0
                self.safety_flag = False
            return True

    def enable_charging(self):
        """
        SAFETY ESCALATION PATH
        """
        if self.safety_flag:
            return False
        if self.state != SlotState.CHARGING:
            return False
        if self.alignment_state != AlignmentState.ALIGNED:
            return False
        return True

    def update_alignment(self, score, features):
        """
        Updates alignment with temporal smoothing and hysteresis.
        """
        # Temporal Smoothing: Low-pass filter (0.7 prev, 0.3 current)
        alpha = 0.3
        self.smoothed_alignment_score = (0.7 * self.smoothed_alignment_score) + (alpha * score)
        
        current_time = time.time()
        ALIGN_THRESHOLD_HIGH = 0.75
        ALIGN_THRESHOLD_LOW = 0.45 
        GRACE_PERIOD = 5.0 # Seconds before we declare MISALIGNED
        
        # Log throttling (1Hz)
        if "overlap_ratio" in features:
            if current_time - self.last_evaluation_time > 1.0:
                logger.info(f"[Slot {self.slot_id+1}] Track {self.locked_track_id} | Overlap: {features['overlap_ratio']:.2f} | Centroid: {features['centroid_score']:.2f} | Final: {score:.2f} | Smoothed: {self.smoothed_alignment_score:.2f}")

        time_in_slot = current_time - self.state_enter_time

        # --- REFINED ALIGNMENT DECISION TREE ---
        if self.smoothed_alignment_score >= ALIGN_THRESHOLD_HIGH:
            # 1. POSITIVE LOCK: Score is good
            if self.alignment_state != AlignmentState.ALIGNED:
                self.alignment_state = AlignmentState.ALIGNED
                self.misalignment_timer = 0.0
                logger.info(f"[Slot {self.slot_id+1}] Alignment Locked: ALIGNED")
                
        elif time_in_slot < GRACE_PERIOD:
            # 2. GRACE PERIOD: Allow adjustment (keep in ALIGNING/Orange-Blue state)
            # Exception: If we were already ALIGNED, allow a bit of wiggle room (hysteresis)
            if self.alignment_state == AlignmentState.ALIGNED:
                if self.smoothed_alignment_score < ALIGN_THRESHOLD_LOW:
                    # Dropped way too low even during grace
                    self.alignment_state = AlignmentState.MISALIGNED
            else:
                self.alignment_state = AlignmentState.STABILIZING
        
        else:
            # 3. DECISION TIME: Grace period over and score < HIGH
            # If we were already Aligned, we have a separate 2s "hysteresis" buffer
            if self.alignment_state == AlignmentState.ALIGNED:
                if self.smoothed_alignment_score < ALIGN_THRESHOLD_LOW:
                    if self.misalignment_timer == 0.0:
                        self.misalignment_timer = current_time
                    elif current_time - self.misalignment_timer > 2.0:
                        self.alignment_state = AlignmentState.MISALIGNED
                        logger.warning(f"[Slot {self.slot_id+1}] Alignment Lost: MISALIGNED")
            else:
                # Never reached HIGH and grace period expired
                self.alignment_state = AlignmentState.MISALIGNED
                logger.warning(f"[Slot {self.slot_id+1}] Decision: MISALIGNED (Grace Period Expired)")

    def handle_occlusion(self, is_occluded):
        current_time = time.time()
        if is_occluded:
            if self.occlusion_timer == 0.0:
                self.occlusion_timer = current_time
                logger.info(f"[Slot {self.slot_id+1}] Vehicle Occluded - Freezing state.")
            elif current_time - self.occlusion_timer > 10.0:
                logger.error(f"[Slot {self.slot_id+1}] Occlusion Timeout. Releasing slot.")
                self.set_state(SlotState.FREE)
        else:
            self.occlusion_timer = 0.0
