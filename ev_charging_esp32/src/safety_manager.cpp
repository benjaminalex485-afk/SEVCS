#include "safety_manager.h"
#include "config.h"
#include "hardware_control.h"

static unsigned long last_heartbeat = 0;
static bool vehicle_present = false;
static bool safe_mode_active = false;

void safety_init() {
    last_heartbeat = millis();
}

void safety_feed_watchdog() {
    last_heartbeat = millis();
    safe_mode_active = false;
}

void safety_update_vehicle_presence(bool present) {
    vehicle_present = present;
}

void safety_process_watchdog() {
    if (millis() - last_heartbeat > HEARTBEAT_TIMEOUT_MS) {
        if (!safe_mode_active) {
            Serial.println("[SAFETY] Watchdog Timeout! Entering SAFE MODE.");
            safe_mode_active = true;
            hw_set_relay(false); // Shutdown relay immediately
            hw_set_led(LED_BLINK_FAST);
        }
    }

    if (!vehicle_present && hw_get_relay()) {
        Serial.println("[SAFETY] Vehicle Disappeared! Emergency Shutdown.");
        hw_set_relay(false);
    }
}

bool safety_is_system_safe() {
    return !safe_mode_active && vehicle_present;
}
