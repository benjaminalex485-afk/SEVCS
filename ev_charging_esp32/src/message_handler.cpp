#include "message_handler.h"
#include "config.h"
#include "hardware_control.h"
#include "safety_manager.h"
#include <ArduinoJson.h>

void msg_handle_incoming(const char* payload) {
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);

    if (error) {
        Serial.print("[MSG] JSON Deserialization failed: ");
        Serial.println(error.c_str());
        return;
    }

    // Schema Validation
    if (!doc.containsKey("slot_id") || doc["slot_id"] != SLOT_ID) return;

    if (doc["command"] == "SET_STATE") {
        bool v_present = doc["vehicle_present"] | false;
        String state = doc["state"] | "IDLE";
        
        safety_update_vehicle_presence(v_present);
        safety_feed_watchdog();

        if (state == "CHARGING" && safety_is_system_safe()) {
            hw_set_relay(true);
            hw_set_led(LED_ON);
        } else {
            hw_set_relay(false);
            hw_set_led(LED_OFF);
        }
    }
}

String msg_create_status_update() {
    StaticJsonDocument<256> doc;
    doc["slot_id"] = SLOT_ID;
    doc["relay"] = hw_get_relay();
    doc["status"] = safety_is_system_safe() ? "OK" : "ERROR";
    doc["uptime_ms"] = millis();
    doc["timestamp"] = 0; // Backend should fill this if NTP is not synced on ESP

    String output;
    serializeJson(doc, output);
    return output;
}
