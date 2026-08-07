"""
i_ckam server — WebRTC relay + face detection
ESP32-CAM MJPEG → OpenCV face detection → WebRTC → browser

Usage:
  python main.py --esp http://192.168.88.XXX:81/stream
"""

import argparse
import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from av import VideoFrame
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("i_ckam")

# ── Config ────────────────────────────────────────────────────────────────────
ESP_STREAM_URL = os.getenv("ESP_URL", "http://192.168.88.XXX:81/stream")
FACE_DB_PATH = Path("faces.json")

# ── Face database (simple JSON) ───────────────────────────────────────────────
def load_face_db():
    if FACE_DB_PATH.exists():
        return json.loads(FACE_DB_PATH.read_text())
    return {"allowed": [], "denied": []}

def save_face_db(db):
    FACE_DB_PATH.write_text(json.dumps(db, indent=2))

# ── MJPEG reader ──────────────────────────────────────────────────────────────
class MJPEGReader:
    """Captures MJPEG frames in a background thread so WebRTC recv() never blocks."""

    def __init__(self, url: str):
        self.url = url
        self._frame = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            url = self.url
            log.info("Camera connecting: %s", url)

            # Probe: check Content-Type to decide stream vs snapshot polling
            mode = self._probe(url)
            if mode == "snapshot":
                self._poll_snapshots(url)
            else:
                self._read_mjpeg(url)

            if self.url != url:
                with self._lock:
                    self._frame = None
            else:
                time.sleep(2)

    def _probe(self, url):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=4) as r:
                ct = r.headers.get("Content-Type", "")
                return "snapshot" if "image/jpeg" in ct else "mjpeg"
        except Exception:
            return "mjpeg"  # let VideoCapture try

    def _read_mjpeg(self, url):
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        cap.open(url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        while self.url == url:
            ret, frame = cap.read()
            if not ret:
                log.warning("Frame read failed, reconnecting…")
                break
            with self._lock:
                self._frame = frame
        cap.release()

    def _poll_snapshots(self, url):
        log.info("Snapshot polling mode: %s", url)
        while self.url == url:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    data = r.read()
                arr = np.frombuffer(data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    with self._lock:
                        self._frame = frame
            except Exception as e:
                log.warning("Snapshot fetch failed: %s", e)
                time.sleep(1)
            time.sleep(0.04)  # ~25 fps

    def read(self):
        with self._lock:
            if self._frame is not None:
                return True, self._frame.copy()
        return False, None

    def release(self):
        with self._lock:
            self._frame = None

# ── WebRTC video track with face detection ────────────────────────────────────
class FaceControlTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, reader: MJPEGReader):
        super().__init__()
        self.reader = reader
        self.face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self.face_events = []  # shared list for WebSocket broadcast

    def _detect_and_draw(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50)
        )
        detections = []
        for (x, y, w, h) in faces:
            detections.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 220, 100), 2)
            cv2.putText(
                frame, "UNKNOWN",
                (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 100), 2
            )
        if detections:
            self.face_events.append({
                "ts": time.time(),
                "faces": detections,
            })
            if len(self.face_events) > 100:
                self.face_events.pop(0)
        return frame

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        ret, frame = self.reader.read()

        if ret and frame is not None:
            frame = self._detect_and_draw(frame)
        else:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(
                frame, "No camera signal",
                (160, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 60, 60), 2
            )

        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="i_ckam")
app.mount("/static", StaticFiles(directory="static"), name="static")

peer_connections: set[RTCPeerConnection] = set()
mjpeg_reader = MJPEGReader(ESP_STREAM_URL)
face_track = FaceControlTrack(mjpeg_reader)
ws_clients: list[WebSocket] = []

@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("static/index.html").read_text()

# WebRTC offer/answer
class RTCOffer(BaseModel):
    sdp: str
    type: str

@app.post("/rtc/offer")
async def rtc_offer(offer: RTCOffer):
    pc = RTCPeerConnection()
    peer_connections.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        log.info("PeerConnection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            peer_connections.discard(pc)

    pc.addTrack(face_track)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return JSONResponse({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    })

# WebSocket for face events (detection feed to UI)
@app.websocket("/ws/events")
async def events_ws(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            await asyncio.sleep(0.3)
            if face_track.face_events:
                ev = face_track.face_events[-1]
                await ws.send_json(ev)
    except WebSocketDisconnect:
        ws_clients.remove(ws)

# Face DB API
@app.get("/api/faces")
def get_faces():
    return load_face_db()

class ConfigUpdate(BaseModel):
    esp_url: str

@app.get("/api/config")
def get_config():
    return {"esp_url": mjpeg_reader.url}

@app.post("/api/config")
def set_config(cfg: ConfigUpdate):
    mjpeg_reader.url = cfg.esp_url.strip()
    mjpeg_reader.release()
    log.info("ESP stream URL updated: %s", mjpeg_reader.url)
    return {"esp_url": mjpeg_reader.url}

class FaceEntry(BaseModel):
    name: str
    list: str  # "allowed" or "denied"

@app.post("/api/faces")
def add_face(entry: FaceEntry):
    db = load_face_db()
    other = "denied" if entry.list == "allowed" else "allowed"
    if entry.name in db[other]:
        db[other].remove(entry.name)
    if entry.name not in db[entry.list]:
        db[entry.list].append(entry.name)
    save_face_db(db)
    return db

@app.delete("/api/faces/{name}")
def remove_face(name: str):
    db = load_face_db()
    db["allowed"] = [n for n in db["allowed"] if n != name]
    db["denied"] = [n for n in db["denied"] if n != name]
    save_face_db(db)
    return db

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--esp", default=ESP_STREAM_URL, help="ESP32-CAM stream URL")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    mjpeg_reader.url = args.esp
    log.info("ESP stream: %s", args.esp)
    log.info("Portal:     http://localhost:%d", args.port)

    uvicorn.run(app, host=args.host, port=args.port)
