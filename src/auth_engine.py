import time
import threading
import logging
import random
import string

logger = logging.getLogger(__name__)

class AuthEngine:
    def __init__(self):
        self.lock = threading.Lock()
        # slot_id -> {auth_code, expires_at, status: "PENDING"|"CONSUMED"}
        self.bookings = {}
        # slot_id -> {track_id, validated_at, status: "AUTHORIZED"|"NONE"}
        self.authorizations = {}

    def generate_booking(self, slot_id, user, timeout=600):
        """
        Generates a 6-digit numeric auth code for a slot.
        """
        code = "".join(random.choices(string.digits, k=6))
        expires_at = time.monotonic() + timeout
        
        with self.lock:
            self.bookings[slot_id] = {
                "auth_code": code,
                "expires_at": expires_at,
                "user": user,
                "status": "PENDING"
            }
            logger.info(f"[AUTH] CODE_GENERATED for Slot {slot_id+1}: {code} (expires in {timeout}s)")
            # Clear any existing authorization for this slot when a new booking is made
            if slot_id in self.authorizations:
                del self.authorizations[slot_id]
        
        return code

    def authorize_vehicle(self, slot_id, code, current_track_id):
        """
        IDEMPOTENT: Validates code, expiry, and binds to track_id.
        """
        now = time.monotonic()
        with self.lock:
            # 1. Check idempotency
            existing = self.authorizations.get(slot_id)
            if existing and existing["status"] == "AUTHORIZED" and existing["track_id"] == current_track_id:
                # If already authorized for same car/code, just return success
                booking = self.bookings.get(slot_id)
                if booking and booking["auth_code"] == code:
                    return "success", True # success, idempotent=True

            # 2. Validate Booking
            booking = self.bookings.get(slot_id)
            if not booking:
                return "wrong_slot", False
            
            if booking["status"] == "CONSUMED":
                return "stale_request", False # Code already used
                
            if now > booking["expires_at"]:
                logger.warning(f"[AUTH] REJECTED: Code expired for Slot {slot_id+1}")
                return "expired", False
                
            if booking["auth_code"] != code:
                logger.warning(f"[AUTH] REJECTED: Invalid code for Slot {slot_id+1}")
                return "invalid_code", False
            
            # 3. Create Authorization (Bind to Identity)
            self.authorizations[slot_id] = {
                "track_id": current_track_id,
                "validated_at": now,
                "status": "AUTHORIZED"
            }
            logger.info(f"[AUTH] VALIDATED Slot {slot_id+1} for Track {current_track_id}")
            return "success", False

    def is_authorized(self, slot_id, track_id):
        """
        Thread-safe check if a specific track_id is authorized for a slot.
        """
        with self.lock:
            auth = self.authorizations.get(slot_id)
            if not auth or auth["status"] != "AUTHORIZED":
                return False
            
            if auth["track_id"] != track_id:
                logger.warning(f"[AUTH] TRACK_MISMATCH for Slot {slot_id+1}: Auth={auth['track_id']}, Current={track_id}")
                return False
                
            # Double check booking expiry even if authorized
            booking = self.bookings.get(slot_id)
            if not booking or time.monotonic() > booking["expires_at"]:
                logger.warning(f"[AUTH] EXPIRED while authorized for Slot {slot_id+1}")
                return False
                
            return True

    def is_expired(self, slot_id):
        with self.lock:
            booking = self.bookings.get(slot_id)
            if not booking: return False
            return time.monotonic() > booking["expires_at"]

    def consume_booking(self, slot_id):
        """
        Finalizes the booking to prevent reuse.
        """
        with self.lock:
            if slot_id in self.bookings:
                self.bookings[slot_id]["status"] = "CONSUMED"
                logger.info(f"[AUTH] CONSUMED Booking for Slot {slot_id+1}")
            if slot_id in self.authorizations:
                self.authorizations[slot_id]["status"] = "NONE"

    def revoke_authorization(self, slot_id):
        with self.lock:
            if slot_id in self.authorizations:
                self.authorizations[slot_id]["status"] = "NONE"
                logger.info(f"[AUTH] CANCELLED Slot {slot_id+1}")

    def clear_all(self):
        with self.lock:
            self.bookings.clear()
            self.authorizations.clear()
            logger.info("[AUTH] All states CLEARED (Reset)")
