#ifndef CONFIG_H
#define CONFIG_H

// WiFi Configuration
#define WIFI_SSID "Your_SSID"
#define WIFI_PASSWORD "Your_PASSWORD"

// WebSocket Server Configuration (PC Backend)
#define WS_SERVER_IP "192.168.1.4"
#define WS_SERVER_PORT 5001
#define WS_SERVER_PATH "/ws/esp32"

// Hardware Configuration
#define PIN_RELAY 18
#define PIN_LED_STATUS 2
#define SLOT_ID 1

// Safety Parameters
#define HEARTBEAT_TIMEOUT_MS 5000
#define STATUS_REPORT_INTERVAL_MS 1000

#endif // CONFIG_H
