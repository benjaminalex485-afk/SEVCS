#include <Arduino.h>
#include "config.h"
#include "wifi_manager.h"
#include "websocket_manager.h"
#include "hardware_control.h"
#include "safety_manager.h"
#include "message_handler.h"

// --- RTOS Tasks ---
void TaskWebSocket(void *pvParameters) {
    for (;;) {
        ws_loop();
        vTaskDelay(pdMS_TO_TICKS(10)); // Yield to other tasks
    }
}

void TaskSafety(void *pvParameters) {
    for (;;) {
        safety_process_watchdog();
        hw_update_led();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void TaskStatusReport(void *pvParameters) {
    for (;;) {
        if (ws_is_connected()) {
            ws_send(msg_create_status_update());
        }
        vTaskDelay(pdMS_TO_TICKS(STATUS_REPORT_INTERVAL_MS));
    }
}

void setup() {
    Serial.begin(115200);
    
    // Initialize Modules
    hw_init();
    safety_init();
    wifi_init();
    ws_init();

    Serial.println("[MAIN] System Modules Initialized.");

    // Create FreeRTOS Tasks
    xTaskCreatePinnedToCore(TaskWebSocket, "WS_Task", 8192, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(TaskSafety, "Safety_Task", 4096, NULL, 3, NULL, 1);
    xTaskCreatePinnedToCore(TaskStatusReport, "Status_Task", 4096, NULL, 1, NULL, 1);
    
    Serial.println("[MAIN] RTOS Tasks Started.");
}

void loop() {
    // Standard loop can handle WiFi reconnection as it's less time-critical
    wifi_loop();
    vTaskDelay(pdMS_TO_TICKS(1000));
}
