#include "wifi_manager.h"
#include "config.h"
#include <WiFi.h>

void wifi_init() {
    Serial.println("[WiFi] Starting Production Dual-Mode (AP+STA)...");
    WiFi.mode(WIFI_AP_STA); 
    delay(2000); 
    
    // Max power for industrial environments
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    
    // Start AP (No password for easy testing)
    WiFi.softAP("SEVCS-PROD-AP", NULL); 
    Serial.print("[WiFi] AP Started. IP: ");
    Serial.println(WiFi.softAPIP());

    // Connect to Vision PC
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.println("[WiFi] Searching for Vision PC...");
}

void wifi_init_ap() {
    // This is now integrated into wifi_init() for atomicity
}

bool wifi_is_connected() {
    return WiFi.status() == WL_CONNECTED;
}

void wifi_loop() {
    static unsigned long last_check = 0;
    if (millis() - last_check > 10000) {
        last_check = millis();
        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("[WiFi] Station not connected. AP is still active.");
        }
    }
}
