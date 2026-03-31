# EV Charging Station Simulator

This repository contains the complete software stack for a simulated Electric Vehicle (EV) Charging Station. It features a modern, mobile-responsive web dashboard with real-time charting, layered on top of a robust ESP32 C++ monolithic backend.

## 📂 Architecture Overview

To maintain simplicity, the entire system is condensed down to a fully functional monolithic architecture:

- `ev_charging_sim.ino`: The main and **only** C++ file required for the ESP32. It handles Wi-Fi AP Mode execution, asynchronous web-server REST routing, role-based Auth via the SD card, hardware simulation physics, dynamic fault injection, and HTTP Client polling to external cameras.
- `data/`: Contains the front-end SPA (Single Page Application). It consists of Vanilla Javascript (`app.js`), CSS (`style.css`), HTML (`index.html`), and a local chart library. This folder is uploaded directly to the ESP32 LittleFS partition and served statically.
- `mock_server.py`: A Python-based Flask server matching the ESP32's REST API precisely. This allows for lightning-fast UI development and UX testing directly on a PC without needing to constantly re-flash the ESP32 hardware.
- `requirements.txt`: Used to install python dependencies (`pip install -r requirements.txt`).

---

## 🚀 How to Work the Code

### 1. Developing and Testing Locally on PC
The absolute best way to iterate on this codebase or test UI changes is by running the local mock server. It provides 1:1 parity with the hardware system.

1. **Install python requirements**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Start the server**:
   ```bash
   python mock_server.py
   ```
3. Open your browser and navigate to `http://localhost:5000`.
4. Log in using the default mock credentials: **Username:** `admin` | **Password:** `admin`.
5. Any modifications to `data/app.js` or `data/index.html` will be immediately reflected upon browser refresh.

### 2. Deploying to the ESP32 Hardware
When you are ready to flash the firmware to the physical microcontroller, follow these steps:

1. Open `ev_charging_sim.ino` in your **Arduino IDE**.
2. Ensure you have the **ESP32 Core (2.0.x)** installed in your Boards Manager.
3. Install the required external library via the Library Manager: `ArduinoJson` (v6.x or newer).
4. Select your board (e.g., `ESP32 WROOM DA Module` or `NodeMCU-32S`).
5. **Critical Step**: You must upload the Frontend to the chip's filesystem. Under `Tools`, select **ESP32 Sketch Data Upload** to format and copy the contents of the `data/` folder directly into the ESP32's memory partition. *(If you don't do this, the ESP32 will throw a 404 error when navigating to the admin panel).*
6. **Upload** the Sketch to compile and flash the native C++ code.
7. Connect your mobile phone or laptop to the dynamically generated Wi-Fi Network: `ESP32-EV-AP`.
8. Open your browser to `http://192.168.4.1` and log in (default credentials: `admin` / `admin`).

---

## ⚙️ Core System Features

- **Auth Management**: Supports adding up to 8 unique users with Admin or standard User privileges. Credentials survive reboots by being parsed from the SD Card via SPI (`/users.json`).
- **Semi-Realistic Charging Simulation**: Features variable current limits, a constant-current bulk phase (up to 80% battery), and tapered finishing current as the charge cycle concludes. 
- **Real-Time Instrumentation**: The Web UI streams dynamic wattage and energy consumption using asynchronous `fetch` requests polling every 2 seconds, painting a visually elegant offline `Chart.js` graph.
- **External Camera Extensibility**: Built-in HTTPClient networking code allows the ESP32 to poll 3rd-party camera servers over the LAN to report connection status and active detection logic directly back to the Charging Dashboard.
