# Smart EV Charging Slot Detection System

This repository contains a full-stack EV Charging management system, integrating **YOLOv8 Computer Vision** for vehicle detection and an **ESP32 Firmware** for simulation and hardware control.

---

## 🚀 Quick Setup (VS Code)

### 1. Automated Setup (Recommended)
If you are on Windows, simply run the provided setup script. This script automatically selects a compatible Python version (3.12 or 3.13) and installs all necessary libraries locally:
```powershell
.\setup_windows.bat
```

### 2. Manual Setup
1. Open the project folder in VS Code.
2. Create a virtual environment and install dependencies:
   ```powershell
   py -3.12 -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```

> [!TIP]
> **Python 3.14 Compatibility**: If your PC has Python 3.14 installed, do NOT use it for this project.
> 
> **How to install the correct version (3.12):**
> 1. Download the **Python 3.12.x Windows Installer (64-bit)** from [python.org](https://www.python.org/downloads/windows/).
> 2. **Important**: When installing, check the box that says **"Add Python to PATH"**.
> 3. After installation, open a NEW terminal and run: `py -3.12 -m venv venv`.
> 4. Run `.\setup_windows.bat` to complete the install.

### 3. Run the Vision Backend
   ```powershell
   .\run.bat
   ```
   - Press **`s`** to draw charging slots on the camera feed.
   - Press **`z`** to draw the queue zone.

---

## 🔌 ESP32 Firmware & Flashing

The firmware is located in the `ev_charging_sim/` directory.

### 1. Board Configuration
- **Board**: `ESP32 Dev Module`
- **Flash Frequency**: `80MHz` / **Upload Speed**: `921600`
- **COM Port**: Check your Device Manager (e.g., `COM3`).

### 2. Flashing Steps
> [!IMPORTANT]
> **This project requires a separate filesystem upload.**
1. **Flash Code**: Open `ev_charging_sim/ev_charging_sim.ino` and click the **Upload** arrow in VS Code.
2. **Flash Filesystem (LittleFS)**: Open the project in the **Arduino IDE** and select **Tools > ESP32 Sketch Data Upload**. This writes the `data/` folder (Web UI) to the chip.

---

## 🎮 Controls & Monitoring

### Keyboard Shortcuts
| Key | Function |
| :---: | :--- |
| **`s`** | **Slot Config**: Left-Click for points, Right-Click to close. |
| **`z`** | **Queue Config**: Define the vehicle waiting area. |
| **`1-9`** | **Input**: Set Energy (kWh) and Rate (kW) for a slot. |
| **`q`** | **Exit/Save**: Quit mode and save configurations. |

### Web Dashboard
Once the ESP32 is flashed and running:
1. Connect to Wi-Fi: `ESP32-EV-AP`.
2. Open browser: `http://192.168.4.1`.
3. Default Login: `admin` / `admin`.

---

## 📁 System Architecture
- `main.py`: Entry point for the YOLOv8 vision system.
- `src/`: Core logic (Detector, Alignment Engine, Queue Manager).
- `ev_charging_sim/`: ESP32 C++ source code and Web Dashboard files.
- `config.yaml`: Persistent storage for slots and zones.
- `models/`: YOLOv8 detection weights.

---

## ❓ Frequently Asked Questions

**Q: Should I uninstall Python 3.14?**
**A: No.** Windows supports multiple versions of Python side-by-side. You can keep 3.14 for other tools. Our `setup_windows.bat` and `run.bat` are designed to specifically target the stable **3.12** environment, so they will not conflict with your other installations.

**Q: How do I know if I'm using the right version?**
**A: Check the terminal.** When you run `.\run.bat`, it activates the `venv`. You can verify the version inside the environment by typing `python --version`. It should say `Python 3.12.x`.

---

## 📊 Diagnostics
View live alignment metrics in the terminal:
`[Slot 1] Track 12 | State: ALIGNING | Overlap: 0.91 | Final: 0.77`
