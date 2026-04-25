#include "web_server_manager.h"
#include <WebServer.h>
#include <ArduinoJson.h>
#include "hardware_control.h"
#include "safety_manager.h"
#include "ui_bundle.h"  // The virtual filesystem

WebServer server(80);

void handleStatus() {
    StaticJsonDocument<512> doc;
    doc["state"] = hw_get_relay() ? "CHARGING" : "IDLE";
    doc["relay"] = hw_get_relay();
    doc["safety"] = safety_is_system_safe() ? "OK" : "SAFE_MODE";
    doc["uptime"] = millis() / 1000;
    
    String response;
    serializeJson(doc, response);
    server.send(200, "application/json", response);
}

#include <HTTPClient.h>

void handleProxy() {
    IPAddress remoteIP = server.client().remoteIP();
    String url = "http://" + remoteIP.toString() + ":5001" + server.uri();
    
    Serial.print("[MirrorProxy] Reflecting to: ");
    Serial.println(url);
    
    HTTPClient http;
    http.begin(url);
    
    // Forward headers (specifically Authorization for Admin access)
    for (int i = 0; i < server.headers(); i++) {
        http.addHeader(server.headerName(i), server.header(i));
    }
    if (server.hasHeader("Authorization")) {
        http.addHeader("Authorization", server.header("Authorization"));
    }
    if (server.hasHeader("Content-Type")) {
        http.addHeader("Content-Type", server.header("Content-Type"));
    }

    int httpCode;
    if (server.method() == HTTP_POST) {
        httpCode = http.POST(server.arg("plain"));
    } else {
        httpCode = http.GET();
    }

    if (httpCode > 0) {
        server.send(httpCode, http.header("Content-Type"), http.getString());
    } else {
        String errorMsg = "Bad Gateway: " + http.errorToString(httpCode);
        Serial.print("[Proxy ERROR] ");
        Serial.println(errorMsg);
        server.send(502, "text/plain", errorMsg + " (Backend at " + remoteIP.toString() + ":5001)");
    }
    http.end();
}

void web_server_init() {
    // 1. Enable Header Capture for Proxy
    const char * headerkeys[] = {"User-Agent", "Authorization", "Content-Type"} ;
    size_t headerkeyssize = sizeof(headerkeys)/sizeof(char*);
    server.collectHeaders(headerkeys, headerkeyssize);

    // 2. Register API Proxy Routes
    server.on("/api/login", HTTP_POST, handleProxy);
    server.on("/api/signup", HTTP_POST, handleProxy);
    server.on("/api/status", HTTP_GET, handleProxy);
    server.on("/api/book_slot", HTTP_POST, handleProxy);

    // 2. Register Virtual Filesystem Routes
    for (int i = 0; i < ui_file_count; i++) {
        const UIFile& f = ui_files[i];
        server.on(f.path, HTTP_GET, [f]() {
            server.send(200, f.mimeType, f.content);
        });
        
        // Handle Root Redirect
        if (String(f.path) == "/index.html") {
            server.on("/", HTTP_GET, [f]() {
                server.send(200, f.mimeType, f.content);
            });
        }
    }

    server.begin();
    Serial.print("[HTTP] Command Center Active. Hosting ");
    Serial.print(ui_file_count);
    Serial.println(" production files.");
}

void web_server_loop() {
    server.handleClient();
}
