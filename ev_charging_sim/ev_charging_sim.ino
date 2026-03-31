#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <LittleFS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ==========================================
// 1. SIMULATION ENGINE
// ==========================================
class SimulationEngine {
public:
    static void update(unsigned long currentMillis);
    static void startSession();
    static void setTargetCurrent(float target);
    static void stop();
    static float getVoltage();
    static float getCurrent();
    static float getPower();
    static float getEnergy();
    static float getBatteryPct();
private:
    static float voltage, current, power, energy, batteryPct, targetCurrent;
    static bool isActive;
    static unsigned long lastUpdate;
};

float SimulationEngine::voltage = 230.0;
float SimulationEngine::current = 0.0;
float SimulationEngine::power = 0.0;
float SimulationEngine::energy = 0.0;
float SimulationEngine::batteryPct = 20.0;
float SimulationEngine::targetCurrent = 0.0;
bool SimulationEngine::isActive = false;
unsigned long SimulationEngine::lastUpdate = 0;

void SimulationEngine::startSession() {
    isActive = true; energy = 0.0; batteryPct = 20.0; lastUpdate = millis();
}
void SimulationEngine::setTargetCurrent(float target) { targetCurrent = target; }
void SimulationEngine::stop() { isActive = false; current = 0.0; power = 0.0; }

void SimulationEngine::update(unsigned long currentMillis) {
    if (!isActive) return;
    if (currentMillis - lastUpdate >= 1000) {
        float dt = (currentMillis - lastUpdate) / 1000.0;
        lastUpdate = currentMillis;
        if (batteryPct < 80) current = targetCurrent;
        else if (batteryPct < 100) {
            current = targetCurrent * ((100.0 - batteryPct) / 20.0);
            if(current < 1.0) current = 1.0; 
        }
        power = voltage * current;
        energy += (power / 1000.0) * (dt * 50.0 / 3600.0);
        batteryPct += (power / 50000.0) * dt * 50.0 * 100.0; 
        if(batteryPct >= 100.0) { batteryPct = 100.0; current = 0; power = 0; isActive = false; }
    }
}
float SimulationEngine::getVoltage() { return voltage; }
float SimulationEngine::getCurrent() { return current; }
float SimulationEngine::getPower() { return power; }
float SimulationEngine::getEnergy() { return energy; }
float SimulationEngine::getBatteryPct() { return batteryPct; }

// ==========================================
// 2. CHARGING CONTROLLER
// ==========================================
enum ChargingState { CS_IDLE, CS_READY, CS_CHARGING, CS_FAULT, CS_COMPLETE };

class ChargingController {
public:
    static void init(float limit);
    static void update(unsigned long currentMillis);
    static bool startCharging();
    static void stopCharging();
    static void setLimit(float limit);
    static void resetToIdle();
    static void triggerFault();
    static String getStateStr();
    static ChargingState getState();
private:
    static ChargingState state;
    static float currentLimit;
};

ChargingState ChargingController::state = CS_IDLE;
float ChargingController::currentLimit = 16.0;

void ChargingController::init(float limit) { currentLimit = limit; state = CS_IDLE; }
void ChargingController::update(unsigned long currentMillis) {
    if (state == CS_CHARGING && SimulationEngine::getBatteryPct() >= 100.0) {
        state = CS_COMPLETE; SimulationEngine::stop();
    }
}
bool ChargingController::startCharging() {
    if (state == CS_IDLE || state == CS_READY || state == CS_COMPLETE) {
        state = CS_CHARGING;
        SimulationEngine::setTargetCurrent(currentLimit);
        SimulationEngine::startSession();
        return true;
    }
    return false;
}
void ChargingController::stopCharging() {
    if(state == CS_CHARGING) { state = CS_READY; SimulationEngine::stop(); }
}
void ChargingController::setLimit(float limit) {
    currentLimit = limit;
    if (state == CS_CHARGING) SimulationEngine::setTargetCurrent(currentLimit);
}
void ChargingController::resetToIdle() { state = CS_IDLE; SimulationEngine::stop(); }
void ChargingController::triggerFault() { state = CS_FAULT; SimulationEngine::stop(); }
ChargingState ChargingController::getState() { return state; }
String ChargingController::getStateStr() {
    switch(state) {
        case CS_IDLE: return "IDLE";
        case CS_READY: return "READY";
        case CS_CHARGING: return "CHARGING";
        case CS_FAULT: return "FAULT";
        case CS_COMPLETE: return "COMPLETE";
    }
    return "UNKNOWN";
}

// ==========================================
// 3. FAULT MANAGER
// ==========================================
class FaultManager {
public:
    static void update(unsigned long currentMillis);
    static void reset();
    static String getFaultStr();
private:
    static String faultType;
    static unsigned long lastCheck;
};

String FaultManager::faultType = "NONE";
unsigned long FaultManager::lastCheck = 0;

void FaultManager::update(unsigned long currentMillis) {
    if(currentMillis - lastCheck >= 1000) {
        lastCheck = currentMillis;
        if(ChargingController::getState() == CS_CHARGING) {
            float v = SimulationEngine::getVoltage();
            float i = SimulationEngine::getCurrent();
            if(v > 250.0) { faultType = "OVERVOLTAGE"; ChargingController::triggerFault(); }
            else if(v < 200.0) { faultType = "UNDERVOLTAGE"; ChargingController::triggerFault(); }
            else if(i > 32.5) { faultType = "OVERCURRENT"; ChargingController::triggerFault(); }
            if (i > 30.0 && random(100) < 1) { faultType = "OVERTEMPERATURE"; ChargingController::triggerFault(); }
        }
    }
}
void FaultManager::reset() { faultType = "NONE"; }
String FaultManager::getFaultStr() { return faultType; }

// ==========================================
// 4. WIFI MANAGER
// ==========================================
class WiFiManager {
public:
    static void initAP() {
        WiFi.softAP("ESP32-EV-AP", "");
        Serial.print("Access Point started! IP address: ");
        Serial.println(WiFi.softAPIP());
    }
};

// ==========================================
// 5. CAMERA CLIENT
// ==========================================
class CameraClient {
public:
    static void init(String ipAddress);
    static void update(unsigned long currentMillis);
    static bool startDetection();
    static bool stopDetection();
    static bool isOnline();
    static String getState();
    static String getSummary();
    static String getSlots();
    static String book(String payload);
    static void taskCallback(void *pvParameters);
private:
    static String cameraIp;
    static SemaphoreHandle_t dataMutex;
    static bool online;
    static String state;
    static String lastSummary;
    static String lastSlots;
};

String CameraClient::cameraIp = "";
SemaphoreHandle_t CameraClient::dataMutex = NULL;
bool CameraClient::online = false;
String CameraClient::state = "idle";
String CameraClient::lastSummary = "{}";
String CameraClient::lastSlots = "[]";

void CameraClient::init(String ipAddress) { 
    cameraIp = ipAddress; 
    dataMutex = xSemaphoreCreateMutex();
    // Start background task with 8KB stack and low priority (1)
    xTaskCreate(taskCallback, "cameraTask", 8192, NULL, 1, NULL);
}

void CameraClient::update(unsigned long currentMillis) { 
    // No-op: handled by taskCallback
}

void CameraClient::taskCallback(void *pvParameters) {
    while(true) {
        if (cameraIp != "") {
            HTTPClient http;
            http.begin("http://" + cameraIp + "/camera/status");
            http.setTimeout(2000);
            int httpCode = http.GET();
            
            bool newOnline = false;
            String newState = "offline";
            
            if (httpCode == 200) {
                DynamicJsonDocument doc(256);
                deserializeJson(doc, http.getString());
                newOnline = doc["online"] | false;
                newState = doc["state"] | "idle";
            }
            http.end();

            String newSummary = "{}";
            String newSlots = "[]";
            
            http.begin("http://" + cameraIp + ":5001/api/vision/status");
            http.setTimeout(2000);
            int visionCode = http.GET();
            if (visionCode == 200) {
                String payload = http.getString();
                DynamicJsonDocument doc(2048);
                deserializeJson(doc, payload);
                newSummary = "{\"charging\":" + doc["charging_count"].as<String>() + 
                            ",\"reserved\":" + doc["reserved_count"].as<String>() + 
                            ",\"queue\":" + doc["queue_count"].as<String>() + "}";
                serializeJson(doc["slots"], newSlots);
            }
            http.end();

            // Update shared data state under mutex
            if (xSemaphoreTake(dataMutex, portMAX_DELAY)) {
                online = newOnline;
                state = newState;
                lastSummary = newSummary;
                lastSlots = newSlots;
                xSemaphoreGive(dataMutex);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

String CameraClient::getSummary() { 
    String res;
    if (xSemaphoreTake(dataMutex, portMAX_DELAY)) { res = lastSummary; xSemaphoreGive(dataMutex); }
    return res;
}
String CameraClient::getSlots() { 
    String res;
    if (xSemaphoreTake(dataMutex, portMAX_DELAY)) { res = lastSlots; xSemaphoreGive(dataMutex); }
    return res;
}
String CameraClient::book(String payload) {
    if (cameraIp == "") return "{\"status\":\"error\"}";
    HTTPClient http;
    http.begin("http://" + cameraIp + ":5001/api/vision/book");
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(payload);
    String res = (code == 200) ? http.getString() : "{\"status\":\"error\"}";
    http.end();
    return res;
}
bool CameraClient::startDetection() {
    if (cameraIp == "") return false;
    HTTPClient http; http.begin("http://" + cameraIp + "/camera/start");
    int code = http.POST(""); http.end(); return code == 200;
}
bool CameraClient::stopDetection() {
    if (cameraIp == "") return false;
    HTTPClient http; http.begin("http://" + cameraIp + "/camera/stop");
    int code = http.POST(""); http.end(); return code == 200;
}
bool CameraClient::isOnline() { 
    bool res;
    if (xSemaphoreTake(dataMutex, portMAX_DELAY)) { res = online; xSemaphoreGive(dataMutex); }
    return res;
}
String CameraClient::getState() { 
    String res;
    if (xSemaphoreTake(dataMutex, portMAX_DELAY)) { res = state; xSemaphoreGive(dataMutex); }
    return res;
}

// ==========================================
// 6. AUTH MANAGER
// ==========================================
class AuthManager {
public:
    static void init();
    static String authenticate(String username, String password);
    static String getUsersJson();
    static bool addUser(String username, String password, String role);
    static bool deleteUser(String username);
};

void AuthManager::init() {
    if(!LittleFS.exists("/users.json")) {
        File file = LittleFS.open("/users.json", FILE_WRITE);
        if(file) {
            file.print("{\"users\":[{\"username\":\"admin\",\"password\":\"admin\",\"role\":\"admin\"}]}");
            file.close();
        }
    }
}
String AuthManager::authenticate(String username, String password) {
    if(username == "admin" && password == "admin") return "admin";
    File file = LittleFS.open("/users.json", FILE_READ);
    if(file) {
        DynamicJsonDocument doc(1024);
        if(!deserializeJson(doc, file)) {
            JsonArray users = doc["users"].as<JsonArray>();
            for(JsonObject v : users) {
                if(v["username"].as<String>() == username && v["password"].as<String>() == password) {
                    file.close(); return v["role"].as<String>();
                }
            }
        }
        file.close();
    }
    return "";
}
String AuthManager::getUsersJson() {
    File file = LittleFS.open("/users.json", FILE_READ);
    if(file) { String data = file.readString(); file.close(); return data; }
    return "{\"users\":[{\"username\":\"admin\",\"role\":\"admin\"}]}";
}
bool AuthManager::addUser(String username, String password, String role) {
    DynamicJsonDocument doc(1024);
    File file = LittleFS.open("/users.json", FILE_READ);
    if(file) { deserializeJson(doc, file); file.close(); }
    JsonArray users = doc["users"];
    if(users.isNull()) users = doc.createNestedArray("users");
    if(users.size() >= 8) return false;
    for(JsonObject v : users) { if(v["username"].as<String>() == username) return false; }
    JsonObject obj = users.createNestedObject();
    obj["username"] = username; obj["password"] = password; obj["role"] = role;
    file = LittleFS.open("/users.json", FILE_WRITE);
    if(file) { serializeJson(doc, file); file.close(); return true; }
    return false;
}
bool AuthManager::deleteUser(String username) {
    if(username == "admin") return false; 
    DynamicJsonDocument doc(1024);
    File file = LittleFS.open("/users.json", FILE_READ);
    if(file) {
        deserializeJson(doc, file); file.close();
        JsonArray users = doc["users"];
        bool found = false;
        for(int i = 0; i < users.size(); i++) {
            if(users[i]["username"].as<String>() == username) {
                users.remove(i); found = true; break;
            }
        }
        if (found) {
            file = LittleFS.open("/users.json", FILE_WRITE);
            if(file) { serializeJson(doc, file); file.close(); return true; }
        }
    }
    return false;
}

// ==========================================
// 7. SCHEDULER
// ==========================================
class Scheduler {
public:
    static void update(unsigned long currentMillis) { } // MVP stub
};

// ==========================================
// 8. LOCAL LOGGER
// ==========================================
class LocalLogger {
public:
    static void init();
    static void logSession(String user, String start, String end, float energy, String fault);
    static String getRecentLogs();
    static void clearLogs();
private:
    static bool isReady;
};

bool LocalLogger::isReady = false;
void LocalLogger::init() {
    // LittleFS must be initialized in setup()
    isReady = true;
    if (!LittleFS.exists("/session.csv")) {
        File f = LittleFS.open("/session.csv", FILE_WRITE);
        if(f) { f.println("user,start_time,end_time,energy_kwh,cost,fault"); f.close(); }
    }
}
void LocalLogger::logSession(String user, String start, String end, float energy, String fault) {
    if(!isReady) return;
    File f = LittleFS.open("/session.csv", FILE_APPEND);
    if(f) {
        f.printf("%s,%s,%s,%.3f,%.2f,%s\n", user.c_str(), start.c_str(), end.c_str(), energy, energy * 0.15, fault.c_str());
        f.close();
    }
}
String LocalLogger::getRecentLogs() {
    if(!isReady || !LittleFS.exists("/session.csv")) {
        return "[]";
    }
    File f = LittleFS.open("/session.csv", FILE_READ);
    if(!f) return "[]";
    
    // Skip header
    f.readStringUntil('\n');
    
    DynamicJsonDocument doc(4096);
    JsonArray arr = doc.to<JsonArray>();
    
    while(f.available()) {
        String line = f.readStringUntil('\n');
        if(line.length() < 5) continue;
        
        int comma1 = line.indexOf(',');
        int comma2 = line.indexOf(',', comma1 + 1);
        int comma3 = line.indexOf(',', comma2 + 1);
        int comma4 = line.indexOf(',', comma3 + 1);
        int comma5 = line.indexOf(',', comma4 + 1);
        
        JsonObject obj = arr.createNestedObject();
        obj["user"] = line.substring(0, comma1);
        obj["start_time"] = line.substring(comma1 + 1, comma2);
        obj["end_time"] = line.substring(comma2 + 1, comma3);
        obj["energy_kwh"] = line.substring(comma3 + 1, comma4).toFloat();
        obj["fault"] = line.substring(comma5 + 1);
    }
    f.close();
    String out;
    serializeJson(doc, out);
    return out;
}
void LocalLogger::clearLogs() {
    if(LittleFS.exists("/session.csv")) {
        LittleFS.remove("/session.csv");
        init(); // Recreate with header
    }
}

// ==========================================
// 9. SERIAL COMM
// ==========================================
class SerialComm {
public:
    static void process(unsigned long currentMillis);
private:
    static String buffer;
    static unsigned long lastPrint;
};

String SerialComm::buffer = "";
unsigned long SerialComm::lastPrint = 0;

void SerialComm::process(unsigned long currentMillis) {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (buffer.length() > 0) {
                buffer.toUpperCase();
                if (buffer == "START") {
                    if (ChargingController::startCharging()) Serial.println("OK");
                    else Serial.println("ERROR");
                } else if (buffer == "STOP") {
                    ChargingController::stopCharging(); Serial.println("OK");
                } else if (buffer == "STATUS?") {
                    Serial.println(ChargingController::getStateStr());
                }
                buffer = "";
            }
        } else { buffer += c; }
    }
    if (currentMillis - lastPrint >= 5000) {
        lastPrint = currentMillis;
        if(ChargingController::getState() == CS_CHARGING) {
            Serial.printf("V=%.1f,I=%.1f,P=%.1f,E=%.3f\n", 
                SimulationEngine::getVoltage(), 
                SimulationEngine::getCurrent(), 
                SimulationEngine::getPower(), 
                SimulationEngine::getEnergy());
        }
    }
}

// ==========================================
// 10. WEB SERVER
// ==========================================
WebServer server(80);

class WebServerManager {
public:
    static void init();
    static void handleClient() { server.handleClient(); }
private:
    static void setupRoutes();
    static void handleNotFound();
};

void WebServerManager::init() {
    // LittleFS already initialized in setup()
    setupRoutes();
    server.onNotFound(handleNotFound);
    server.begin();
}

void WebServerManager::setupRoutes() {
    server.on("/api/login", HTTP_POST, []() {
        if(server.hasArg("plain") == false) { server.send(400, "application/json", "{\"status\":\"error\"}"); return; }
        DynamicJsonDocument doc(512); deserializeJson(doc, server.arg("plain"));
        String role = AuthManager::authenticate(doc["username"]|"", doc["password"]|"");
        if(role != "") server.send(200, "application/json", "{\"status\":\"success\", \"role\":\"" + role + "\"}");
        else server.send(401, "application/json", "{\"status\":\"error\", \"message\":\"Invalid credentials\"}");
    });
    server.on("/api/status", HTTP_GET, []() {
        DynamicJsonDocument doc(512);
        doc["state"] = ChargingController::getStateStr();
        doc["voltage"] = SimulationEngine::getVoltage();
        doc["current"] = SimulationEngine::getCurrent();
        doc["power"] = SimulationEngine::getPower();
        doc["energy"] = SimulationEngine::getEnergy();
        doc["battery_pct"] = SimulationEngine::getBatteryPct();
        doc["fault_type"] = FaultManager::getFaultStr();
        doc["uptime"] = millis() / 1000;
        String response; serializeJson(doc, response);
        server.send(200, "application/json", response);
    });
    server.on("/api/start", HTTP_POST, []() {
        if(ChargingController::startCharging()) server.send(200, "application/json", "{\"status\":\"success\"}");
        else server.send(400, "application/json", "{\"status\":\"error\", \"message\":\"Cannot start\"}");
    });
    server.on("/api/stop", HTTP_POST, []() {
        ChargingController::stopCharging(); server.send(200, "application/json", "{\"status\":\"success\"}");
    });
    server.on("/api/set_current", HTTP_POST, []() {
        if(server.hasArg("plain")) {
            DynamicJsonDocument doc(256); deserializeJson(doc, server.arg("plain"));
            if(doc.containsKey("limit")) {
                ChargingController::setLimit(doc["limit"]);
                server.send(200, "application/json", "{\"status\":\"success\"}"); return;
            }
        }
        server.send(400, "application/json", "{\"status\":\"error\"}");
    });
    server.on("/api/reset_fault", HTTP_POST, []() {
        FaultManager::reset(); ChargingController::resetToIdle();
        server.send(200, "application/json", "{\"status\":\"success\"}");
    });
    server.on("/api/logs", HTTP_GET, []() { server.send(200, "application/json", LocalLogger::getRecentLogs()); });
    server.on("/api/logs/clear", HTTP_POST, []() {
        LocalLogger::clearLogs();
        server.send(200, "application/json", "{\"status\":\"success\"}");
    });
    
    server.on("/api/camera/status", HTTP_GET, []() {
        DynamicJsonDocument doc(256);
        doc["online"] = CameraClient::isOnline(); doc["state"] = CameraClient::getState();
        String response; serializeJson(doc, response);
        server.send(200, "application/json", response);
    });
    server.on("/api/camera/stop", HTTP_POST, []() {
        if(CameraClient::stopDetection()) server.send(200, "application/json", "{\"status\":\"success\"}");
        else server.send(500, "application/json", "{\"status\":\"error\"}");
    });

    server.on("/api/station/summary", HTTP_GET, []() {
        server.send(200, "application/json", CameraClient::getSummary());
    });
    server.on("/api/slots", HTTP_GET, []() {
        server.send(200, "application/json", CameraClient::getSlots());
    });
    server.on("/api/book", HTTP_POST, []() {
        if(server.hasArg("plain")) {
            String res = CameraClient::book(server.arg("plain"));
            server.send(200, "application/json", res);
        } else {
            server.send(400, "application/json", "{\"status\":\"error\"}");
        }
    });
    server.on("/api/users", HTTP_GET, []() { server.send(200, "application/json", AuthManager::getUsersJson()); });
    server.on("/api/users", HTTP_POST, []() {
        if(server.hasArg("plain")) {
            DynamicJsonDocument doc(256); deserializeJson(doc, server.arg("plain"));
            String u = doc["username"]|""; String p = doc["password"]|""; String r = doc["role"]|"user";
            if(u.length() > 0 && p.length() > 0) {
                if(AuthManager::addUser(u, p, r)) { server.send(200, "application/json", "{\"status\":\"success\"}"); return; }
            }
        }
        server.send(400, "application/json", "{\"status\":\"error\", \"message\":\"Failed to add user (limit 8 or bad input)\"}");
    });
    server.on("/api/users", HTTP_DELETE, []() {
        if(server.hasArg("plain")) {
            DynamicJsonDocument doc(256); deserializeJson(doc, server.arg("plain"));
            String u = doc["username"]|"";
            if(u.length() > 0) {
                if(AuthManager::deleteUser(u)) { server.send(200, "application/json", "{\"status\":\"success\"}"); return; }
            }
        }
        server.send(400, "application/json", "{\"status\":\"error\"}");
    });
}
void WebServerManager::handleNotFound() {
    String path = server.uri();
    if(path.endsWith("/")) path += "index.html";
    
    // Prioritize GZIP if available
    String gzPath = path + ".gz";
    if (LittleFS.exists(gzPath)) {
        File file = LittleFS.open(gzPath, "r");
        server.sendHeader("Content-Encoding", "gzip");
        String contentType = "text/plain";
        if(path.endsWith(".html")) contentType = "text/html";
        else if(path.endsWith(".css")) contentType = "text/css";
        else if(path.endsWith(".js")) contentType = "application/javascript";
        server.streamFile(file, contentType);
        file.close();
        return;
    }

    if(LittleFS.exists(path)) {
        String contentType = "text/plain";
        if(path.endsWith(".html")) contentType = "text/html";
        else if(path.endsWith(".css")) contentType = "text/css";
        else if(path.endsWith(".js")) contentType = "application/javascript";
        File file = LittleFS.open(path, "r"); server.streamFile(file, contentType); file.close();
    } else {
        File file = LittleFS.open("/index.html", "r");
        if(file) { server.streamFile(file, "text/html"); file.close(); }
        else { server.send(404, "text/plain", "File Not Found and No SPA Index"); }
    }
}

// ==========================================
// MAIN SETUP AND LOOP
// ==========================================
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n--- EV Charging Station Simulator Booting ---");

    if(!LittleFS.begin(true)){ Serial.println("LittleFS Error"); return; }

    LocalLogger::init();
    AuthManager::init(); 
    
    ChargingController::init(16.0); // Default 16A limit
    CameraClient::init("192.168.4.2");

    WiFiManager::initAP();
    WebServerManager::init();

    Serial.println("System Initialized. AP IP: 192.168.4.1");
}

void loop() {
    unsigned long currentMillis = millis();

    WebServerManager::handleClient();
    SerialComm::process(currentMillis);
    
    FaultManager::update(currentMillis);
    Scheduler::update(currentMillis);
    SimulationEngine::update(currentMillis);
    ChargingController::update(currentMillis);
    CameraClient::update(currentMillis);
    
    delay(2);
}
