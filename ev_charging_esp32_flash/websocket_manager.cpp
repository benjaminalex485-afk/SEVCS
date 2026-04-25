#include "websocket_manager.h"
#include "config.h"
#include "message_handler.h"
#include "wifi_manager.h"
#include <ArduinoWebsockets.h>

using namespace websockets;

WebsocketsClient client;
static bool is_ws_connected = false;

void onMessageCallback(WebsocketsMessage message) {
    msg_handle_incoming(message.data().c_str());
}

void onEventCallback(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionOpened) {
        Serial.println("[WS] Connection Opened");
        is_ws_connected = true;
    } else if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("[WS] Connection Closed");
        is_ws_connected = false;
    }
}

void ws_init() {
    client.onMessage(onMessageCallback);
    client.onEvent(onEventCallback);
}

void ws_loop() {
    if (wifi_is_connected()) {
        if (!is_ws_connected) {
            static unsigned long last_retry = 0;
            if (millis() - last_retry > 5000) {
                last_retry = millis();
                String server_url = "ws://" + String(WS_SERVER_IP) + ":" + String(WS_SERVER_PORT) + String(WS_SERVER_PATH);
                Serial.println("[WS] Attempting connection...");
                Serial.println(server_url);
                client.connect(WS_SERVER_IP, WS_SERVER_PORT, WS_SERVER_PATH);
            }
        }
        client.poll();
    }
}

void ws_send(String message) {
    if (is_ws_connected) {
        client.send(message);
    }
}

bool ws_is_connected() {
    return is_ws_connected;
}
