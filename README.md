# i_ckam

Digital facecontrol for events — ESP32-CAM live stream with face detection, served as a WebRTC web portal.

```
ESP32-CAM  ──MJPEG──▶  Python server  ──WebRTC──▶  Browser portal
                          (face detect)
```

---

## Hardware

| Part | Notes |
|------|-------|
| ESP32-CAM (AI Thinker) | OV2640 camera, FT232 USB adapter for flashing |
| Power | 5V/2A via USB or external supply |

---

## 1 — Flash the ESP32-CAM

### Requirements
- [Arduino IDE 2.x](https://www.arduino.cc/en/software)
- Board package: **ESP32 by Espressif** (Boards Manager → search `esp32`)
- Board: **AI Thinker ESP32-CAM**

### Steps

1. Open `firmware/esp32cam/esp32cam.ino`
2. Edit the WiFi credentials at the top:
   ```cpp
   const char* WIFI_SSID = "YOUR_WIFI_SSID";
   const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
   ```
3. Connect the ESP32-CAM via USB (FT232 adapter), short **IO0 → GND** to enter flash mode
4. Select port (e.g. `/dev/ttyUSB0`) and upload
5. Remove the IO0 bridge, press reset
6. Open Serial Monitor at **115200 baud** — the IP address will print:
   ```
   Connected! IP: 192.168.88.XXX
   Stream:   http://192.168.88.XXX:81/stream
   Snapshot: http://192.168.88.XXX:81/capture
   ```

---

## 2 — Run the server

```bash
cd server
pip install -r requirements.txt
python main.py --esp http://192.168.88.XXX:81/stream
```

Portal opens at **http://localhost:8080**

### Environment variable alternative
```bash
ESP_URL=http://192.168.88.XXX:81/stream python main.py
```

---

## 3 — Web portal

Open `http://localhost:8080` in a browser.

- **CONNECT** — starts the WebRTC stream from the server
- Face detection boxes appear on the video in real-time
- **ALLOW / DENY** — log a face event for the current frame
- Event log shows timestamped entries
- Edit the ESP URL field to change the camera source without restarting the server

---

## Project structure

```
i_ckam/
├── firmware/
│   └── esp32cam/
│       └── esp32cam.ino      # Arduino sketch — WiFi + MJPEG stream
└── server/
    ├── main.py               # FastAPI + aiortc WebRTC relay + face detection
    ├── requirements.txt
    └── static/
        └── index.html        # Web portal (WebRTC + face log UI)
```

---

## Roadmap

- [ ] Face recognition (known vs unknown)
- [ ] VIP / deny list with face embeddings
- [ ] Auto-allow / alert on known faces
- [ ] Multi-camera support
- [ ] Event export (CSV / webhook)

---

## License

MIT
