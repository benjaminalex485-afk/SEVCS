import time
from enum import Enum
import numpy as np
import logging
import cv2
from . import utils

# Configure logging
logger = logging.getLogger(__name__)

class SlotState(Enum):
    FREE = 0
    RESERVED = 1
    ALIGNMENT_PENDING = 2
    AUTH_PENDING = 3
    AUTH_ACTIVE = 4
    CHARGING = 5
    MISALIGNED = 6

class AlignmentState(Enum):
    UNSTABLE = 0
    STABILIZING = 1
    ALIGNED = 2
    MISALIGNED = 3

class SuggestionState(Enum):
    SOFT = 0      # < 1s
    STABLE = 1    # 1-3s
    COMMITTED = 2 # > 3s

class Slot:
    def __init__(self, slot_id, polygon):
        self.slot_id = slot_id
        self.polygon = np.array(polygon, np.int32)
        self.bbox = cv2.boundingRect(self.polygon)
        
        # Precompute centroid
        M = cv2.moments(self.polygon)
        if M["m00"] != 0:
            self.centroid = (int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"]))
        else:
            self.centroid = (int(np.mean(self.polygon[:, 0])), int(np.mean(self.polygon[:, 1])))
        
        # State
        self.state = SlotState.FREE
        self.locked_track_id = None
        self.assigned_track_id = None
        self.reservation_id = None
        self.track_age = 0
        self.safety_flag = False
        
        # Stage 3/4: Suggestions & Hardening
        self.suggested_track_id = None
        self.suggestion_timestamp = 0.0
        self.suggestion_confidence = 0.0
        self.suggestion_state = SuggestionState.SOFT
        
        self.hold_track_id = None
        self.hold_start_time = 0.0
        self.hold_confidence = 0.0
        self.hold_frames = 0
        
        # Alignment
        self.alignment_state = AlignmentState.UNSTABLE
        self.alignment_score = 0.0
        self.smoothed_alignment_score = 0.0
        
        # Timers
        self.last_evaluation_time = 0.0
        self.misalignment_timer = 0.0
        self.occlusion_timer = 0.0
        self.state_enter_time = utils.now()
        self.last_update_time = utils.now()

    def update_hold(self):
        """FPS-aware HOLD decay logic."""
        if self.hold_track_id is None:
            return

        now = utils.now()
        dt = now - self.last_update_time
        self.last_update_time = now
        
        # Exponential decay: ~10 frame half-life at 30 FPS (k=2.0)
        self.hold_confidence *= np.exp(-2.0 * dt)
        self.hold_frames += 1
        
        # Expiry: Time-based (max ~500ms) or frame-based (max 10)
        if self.hold_frames > 10 or now - self.hold_start_time > 0.5:
            logger.debug(f"[Slot {self.slot_id+1}] HOLD Expired for Track {self.hold_track_id}")
            self.hold_track_id = None
            self.hold_confidence = 0.0
            self.hold_frames = 0

    def to_dict(self):
        """Deep isolation snapshot: returns primitive types only."""
        return {
            "slot_id": int(self.slot_id),
            "state": self.state.name,
            "locked_track_id": int(self.locked_track_id) if self.locked_track_id is not None else None,
            "assigned_track_id": int(self.assigned_track_id) if self.assigned_track_id is not None else None,
            "alignment_state": self.alignment_state.name,
            "alignment_score": float(self.alignment_score),
            "smoothed_alignment_score": float(self.smoothed_alignment_score),
            "suggestion": {
                "track_id": self.suggested_track_id,
                "confidence": float(self.suggestion_confidence),
                "state": self.suggestion_state.name,
                "stable_for": float(min(utils.now() - self.suggestion_timestamp, 10.0)) if self.suggestion_timestamp > 0 else 0.0
            },
            "occluded": self.occlusion_timer > 0
        }

    def force_safe_state(self):
        """
        Emergency-only bypass of transition validation.
        Resets slot to FREE and clears all coupling fields.
        """
        logger.critical(f"[Slot {self.slot_id+1}] FORCED SAFE RESET triggered.")
        self.locked_track_id = None
        self.assigned_track_id = None
        self.suggested_track_id = None
        self.track_age = 0
        self.smoothed_alignment_score = 0.0
        self.alignment_state = AlignmentState.UNSTABLE
        self.suggestion_state = SuggestionState.SOFT
        self.suggestion_timestamp = 0
        self.suggestion_confidence = 0.0
        self.state = SlotState.FREE # Emergency bypass
        self.state_enter_time = utils.now()
        self.misalignment_timer = 0.0
        self.occlusion_timer = 0.0

    def is_in_occlusion_debounce(self):
        """Returns True if the vehicle is currently occluded but the grace period hasn't expired."""
        return self.occlusion_timer > 0

    def validate_transition(self, new_state):
        """
        Enforces physical reality by restricting allowed state jumps.
        """
        allowed = {
            SlotState.FREE: [SlotState.RESERVED, SlotState.ALIGNMENT_PENDING],
            SlotState.RESERVED: [SlotState.ALIGNMENT_PENDING, SlotState.FREE],
            SlotState.ALIGNMENT_PENDING: [SlotState.AUTH_PENDING, SlotState.MISALIGNED, SlotState.FREE],
            SlotState.AUTH_PENDING: [SlotState.AUTH_ACTIVE, SlotState.ALIGNMENT_PENDING, SlotState.FREE],
            SlotState.AUTH_ACTIVE: [SlotState.CHARGING, SlotState.ALIGNMENT_PENDING, SlotState.FREE],
            SlotState.CHARGING: [SlotState.MISALIGNED, SlotState.FREE],
            SlotState.MISALIGNED: [SlotState.CHARGING, SlotState.ALIGNMENT_PENDING, SlotState.FREE]
        }
        return new_state in allowed.get(self.state, [])

    def set_state(self, new_state, track_id=None):
        if self.state != new_state:
            if not self.validate_transition(new_state):
                logger.error(f"[Slot {self.slot_id+1}] REJECTED Invalid Transition: {self.state.name} -> {new_state.name}")
                
                # Dynamic strict mode check from a global CONFIG if possible, 
                # but for now we'll stick to basic validation.
                self.force_safe_state()
                return False

            logger.info(f"[Slot {self.slot_id+1}] State Change: {self.state.name} -> {new_state.name} | Track: {track_id}")
            self.state = new_state
            self.state_enter_time = utils.now()
            
            # Atomically update track ID during state transition
            if track_id is not None:
                self.locked_track_id = track_id
            
            return True
        return False

    def update_alignment(self, score, features):
        """
        Updates alignment with temporal smoothing and hysteresis.
        """
        # Temporal Smoothing: Low-pass filter (0.7 prev, 0.3 current)
        alpha = 0.3
        self.smoothed_alignment_score = (0.7 * self.smoothed_alignment_score) + (alpha * score)
        
        current_time = utils.now()
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
        current_time = utils.now()
        if is_occluded:
            if self.occlusion_timer == 0.0:
                self.occlusion_timer = current_time
                logger.info(f"[Slot {self.slot_id+1}] Vehicle Occluded - Freezing state.")
            elif current_time - self.occlusion_timer > 10.0:
                logger.error(f"[Slot {self.slot_id+1}] Occlusion Timeout. Releasing slot.")
                self.set_state(SlotState.FREE)
        else:
            self.occlusion_timer = 0.0

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
