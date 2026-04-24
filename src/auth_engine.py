import time
import threading
import logging
import random
import string
from . import utils

logger = logging.getLogger(__name__)

class AuthEngine:
    def __init__(self):
        self.lock = threading.RLock()
        # slot_id -> {auth_code, expires_at, user, status: "PENDING"|"CLAIMED"|"ACTIVE"|"EXPIRED"}
        self.bookings = {}
        # slot_id -> {track_id, validated_at, status: "AUTHORIZED"|"NONE"}
        self.authorizations = {}
        # Rate limiting: identifier -> [timestamps]
        self.attempts = {}

    def to_dict(self):
        """Deep isolation snapshot: returns primitive types only."""
        with self.lock:
            now = utils.system_now(caller="api_thread")
            return {
                "bookings": {
                    int(sid): {
                        "user": b["user"],
                        "status": b["status"],
                        "expires_in": float(max(0, b["expires_at"] - now))
                    } for sid, b in self.bookings.items()
                },
                "authorizations": {
                    int(sid): {
                        "track_id": a["track_id"],
                        "status": a["status"]
                    } for sid, a in self.authorizations.items()
                }
            }

    def generate_booking(self, slot_id, user, timeout=600):
        """
        Generates a 6-digit numeric auth code for a slot.
        Rejects if a non-expired booking already exists.
        """
        with self.lock:
            # ISSUE 1: Prevent silent overwrite
            existing = self.bookings.get(slot_id)
            if existing and existing["status"] in ["PENDING", "CLAIMED"] and utils.system_now(caller="api_thread") < existing["expires_at"]:
                logger.warning(f"[AUTH] REJECTED: Slot {slot_id+1} already has a valid booking.")
                return None

            code = "".join(random.choices(string.digits, k=6))
            expires_at = utils.system_now(caller="api_thread") + timeout
            
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

    def set_booking_status(self, slot_id, status):
        """Centralized trigger for booking lifecycle transitions."""
        with self.lock:
            if slot_id in self.bookings:
                prev = self.bookings[slot_id]["status"]
                if prev != status:
                    self.bookings[slot_id]["status"] = status
                    logger.info(f"[AUTH] Booking Slot {slot_id+1}: {prev} -> {status}")

    def record_attempt(self, identifier):
        """Records an API attempt timestamp for rate limiting."""
        with self.lock:
            now = utils.system_now(caller="api_thread")
            if identifier not in self.attempts:
                self.attempts[identifier] = []
            self.attempts[identifier].append(now)
            # Cleanup old attempts (> 1 hour) to prevent leak
            self.attempts[identifier] = [t for t in self.attempts[identifier] if now - t < 3600]

    def check_rate_limit(self, identifier, limit, window):
        """Checks if identifier has exceeded limit within window seconds."""
        with self.lock:
            if identifier not in self.attempts:
                return True
            now = utils.system_now(caller="api_thread")
            recent = [t for t in self.attempts[identifier] if now - t < window]
            return len(recent) <= limit

    def authorize_vehicle(self, slot_id, code, current_track_id):
        """
        IDEMPOTENT: Validates code, expiry, and binds to track_id.
        Transitions PENDING -> CLAIMED
        """
        now = utils.system_now(caller="api_thread")
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
            
            if booking["status"] in ["ACTIVE", "EXPIRED"]:
                return "stale_request", False # Code already used or expired
                
            if now > booking["expires_at"]:
                logger.warning(f"[AUTH] REJECTED: Code expired for Slot {slot_id+1}")
                self.bookings[slot_id]["status"] = "EXPIRED"
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
            # TRIGGER: PENDING -> CLAIMED
            self.bookings[slot_id]["status"] = "CLAIMED"
            
            logger.info(f"[AUTH] VALIDATED Slot {slot_id+1} for Track {current_track_id}")
            return "success", False

    def is_authorized(self, slot_id, track_id):
        """
        Thread-safe check if a specific track_id is authorized for a slot.
        Returns: (is_authorized, reason)
        """
        with self.lock:
            auth = self.authorizations.get(slot_id)
            if not auth or auth["status"] != "AUTHORIZED":
                return False, "NO_AUTH"
            
            if auth["track_id"] != track_id:
                logger.warning(f"[AUTH] TRACK_MISMATCH for Slot {slot_id+1}: Auth={auth['track_id']}, Current={track_id}")
                return False, "ID_MISMATCH"
                
            # Double check booking expiry even if authorized
            booking = self.bookings.get(slot_id)
            if not booking or utils.system_now(caller="main_loop") > booking["expires_at"]:
                logger.warning(f"[AUTH] EXPIRED while authorized for Slot {slot_id+1}")
                if booking: self.bookings[slot_id]["status"] = "EXPIRED"
                return False, "EXPIRED"
                
            return True, "VALID"

    def is_expired(self, slot_id):
        with self.lock:
            booking = self.bookings.get(slot_id)
            if not booking: return False
            if booking["status"] == "EXPIRED": return True
            expired = utils.system_now(caller="main_loop") > booking["expires_at"]
            if expired: self.bookings[slot_id]["status"] = "EXPIRED"
            return expired

    def consume_booking(self, slot_id):
        """
        Finalizes the booking to prevent reuse.
        Transitions CLAIMED -> ACTIVE
        """
        with self.lock:
            if slot_id in self.bookings:
                self.bookings[slot_id]["status"] = "ACTIVE"
                logger.info(f"[AUTH] CONSUMED Booking for Slot {slot_id+1} (ACTIVE)")
            if slot_id in self.authorizations:
                self.authorizations[slot_id]["status"] = "NONE"

    def revoke_authorization(self, slot_id):
        """
        Cancels authorization. 
        Transitions CLAIMED -> PENDING (fallback allowed if not active)
        """
        with self.lock:
            if slot_id in self.authorizations:
                self.authorizations[slot_id]["status"] = "NONE"
                logger.info(f"[AUTH] CANCELLED Slot {slot_id+1}")
            
            if slot_id in self.bookings:
                if self.bookings[slot_id]["status"] == "CLAIMED":
                    self.bookings[slot_id]["status"] = "PENDING"
                elif self.bookings[slot_id]["status"] == "ACTIVE":
                    self.bookings[slot_id]["status"] = "EXPIRED"

    def clear_all(self):
        with self.lock:
            self.bookings.clear()
            self.authorizations.clear()
            logger.info("[AUTH] All states CLEARED (Reset)")
