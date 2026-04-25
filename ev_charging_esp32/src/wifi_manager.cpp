#include "wifi_manager.h"
#include "config.h"
#include <WiFi.h>

void wifi_init() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.println("[WiFi] Connecting...");
}

bool wifi_is_connected() {
    return WiFi.status() == WL_CONNECTED;
}

void wifi_loop() {
    static unsigned long last_check = 0;
    if (millis() - last_check > 5000) {
        last_check = millis();
        if (!wifi_is_connected()) {
            Serial.println("[WiFi] Disconnected. Reconnecting...");
            WiFi.disconnect();
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        }
    }
}
