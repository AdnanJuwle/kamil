"""
core/room_watch.py -- Kamil watching the room, always.

On the Fedora box Kamil is not an app you open; it is a presence in the room. This
module is the eye that stays open: the webcam is held for the whole session, every
frame is checked for motion, and anything that moves gets recorded as a clip and
written to a searchable log. When the primary model is multimodal (it is --
Huihui-Qwen3.8-27B-abliterated), a keyframe from each event also goes to the model
so the log reads in words: "Adnan sat down at the desk", not "motion, 14:22:07".

Design points that matter:

* ONE owner of the camera. A V4L2 device generally cannot be opened twice, so a
  24x7 watcher would otherwise lock out the `look` tool. The capture thread holds
  the device and publishes the newest frame through latest_frame_jpeg(); the look
  tool reads from here instead of opening its own handle.

* Inference never blocks capture. Describing a frame with a 27B model takes
  seconds. The capture loop hands keyframes to a worker through a depth-1 slot and
  drops the frame if the worker is busy, so the camera never stalls and chat never
  waits behind the watcher.

* Motion detection stays cheap. Frame differencing against a running average at a
  downscaled resolution -- a few hundred microseconds per frame, no contrib build,
  no model. The model is spent only on events, and only once per cooldown.

* It forgets. Clips roll off after KAMIL_ROOM_RETENTION_DAYS (default 14) so a
  continuously-watching camera cannot quietly fill the disk.

Everything stays on the machine: clips under data/room/, events in
data/room_events.db. Both are gitignored. Nothing is uploaded, ever.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.paths import DATA_DIR, IS_LINUX, IS_WINDOWS


# ── Configuration ──────────────────────────────────────────────────
def _flag(name: str, default: bool) -> bool:
    env = os.getenv(name)
    if env is None:
        return default
    return env.strip().lower() not in ("", "0", "false", "no", "off")


# 24x7 watching is the point on Fedora. Elsewhere Kamil is a guest: off unless asked.
ENABLED = _flag("KAMIL_ROOM_WATCH", IS_LINUX)

CAMERA_INDEX = int(os.getenv("KAMIL_CAMERA_INDEX", "0"))
FPS = float(os.getenv("KAMIL_ROOM_FPS", "6"))              # capture/analysis rate
DETECT_WIDTH = int(os.getenv("KAMIL_ROOM_DETECT_WIDTH", "480"))
PIXEL_THRESHOLD = int(os.getenv("KAMIL_ROOM_THRESHOLD", "22"))   # per-pixel delta
MIN_AREA = int(os.getenv("KAMIL_ROOM_MIN_AREA", "1200"))         # px^2 at DETECT_WIDTH
CLIP_TAIL_S = float(os.getenv("KAMIL_ROOM_CLIP_TAIL", "4"))      # keep rolling after stillness
MAX_CLIP_S = float(os.getenv("KAMIL_ROOM_MAX_CLIP", "120"))      # split long events
MIN_EVENT_S = float(os.getenv("KAMIL_ROOM_MIN_EVENT", "0.6"))    # ignore single-frame blips
VISION_COOLDOWN_S = float(os.getenv("KAMIL_ROOM_VISION_COOLDOWN", "45"))
RETENTION_DAYS = int(os.getenv("KAMIL_ROOM_RETENTION_DAYS", "14"))
WARMUP_FRAMES = int(os.getenv("KAMIL_ROOM_WARMUP", "10"))        # auto-exposure settle

ROOM_DIR = Path(os.getenv("KAMIL_ROOM_DIR") or (DATA_DIR / "room"))
DB_PATH = str(DATA_DIR / "room_events.db")

WATCH_PROMPT = (
    "This is a still frame from a webcam watching a room. In one or two sentences, "
    "say concretely what is happening and who or what is present. Read any visible "
    "text verbatim. Do not speculate about anything you cannot see; if the frame is "
    "dark or empty, say so plainly."
)


# ── Runtime state ──────────────────────────────────────────────────
_lock = threading.Lock()
_thread: threading.Thread | None = None
_describe_thread: threading.Thread | None = None
_stop = threading.Event()

_latest_jpeg: bytes | None = None
_latest_ts: float = 0.0

# depth-1 handoff to the describe worker: newest keyframe wins, stale ones drop
_pending: tuple[int, bytes] | None = None
_pending_cv = threading.Condition(_lock)

_status = {
    "running": False,
    "camera": CAMERA_INDEX,
    "opened": False,
    "recording": False,
    "error": "",
    "frames": 0,
    "events": 0,
    "last_motion": None,
    "last_event_id": None,
    "last_description": "",
    "started_at": None,
}


# ── Event store ────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS room_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            started      TEXT NOT NULL,
            ended        TEXT,
            duration_s   REAL,
            clip_path    TEXT,
            peak_area    INTEGER,
            frames       INTEGER,
            description  TEXT,
            faces        TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_started ON room_events(started)")
    return conn


def _event_open(started: datetime, clip_path: str) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO room_events (started, clip_path) VALUES (?, ?)",
            (started.isoformat(timespec="seconds"), clip_path))
        return int(cur.lastrowid)


def _event_close(event_id: int, ended: datetime, duration: float,
                 peak_area: int, frames: int) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE room_events SET ended=?, duration_s=?, peak_area=?, frames=? "
            "WHERE id=?",
            (ended.isoformat(timespec="seconds"), round(duration, 2),
             int(peak_area), int(frames), event_id))


def _event_describe(event_id: int, description: str, faces: str = "") -> None:
    with _db() as conn:
        conn.execute("UPDATE room_events SET description=?, faces=? WHERE id=?",
                     (description, faces, event_id))


def recent_events(limit: int = 20, since: str | None = None) -> list[dict]:
    """The room's recent history, newest first -- what Kamil recalls about the room."""
    sql = ("SELECT id, started, ended, duration_s, clip_path, peak_area, frames, "
           "description, faces FROM room_events")
    args: list = []
    if since:
        sql += " WHERE started >= ?"
        args.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _db() as conn:
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    cols = ("id", "started", "ended", "duration_s", "clip_path", "peak_area",
            "frames", "description", "faces")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["clip"] = os.path.basename(d["clip_path"] or "")
        out.append(d)
    return out


def summary(hours: int = 12) -> str:
    """A short prose digest of the room, for injection into Kamil's prompt."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    events = recent_events(limit=12, since=since)
    if not events:
        return ""
    lines = [f"Room activity (webcam, last {hours}h, {len(events)} motion events):"]
    for e in events[:8]:
        when = (e["started"] or "")[11:16]
        what = (e["description"] or "").strip() or "motion, not yet described"
        lines.append(f"  {when} - {what}")
    return "\n".join(lines)


def _purge_old() -> int:
    """Drop clips and rows past the retention window. Returns files deleted."""
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    removed = 0
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, clip_path FROM room_events WHERE started < ?",
                (cutoff,)).fetchall()
            for _id, clip in rows:
                if clip and os.path.isfile(clip):
                    try:
                        os.remove(clip)
                        removed += 1
                    except OSError:
                        pass
            conn.execute("DELETE FROM room_events WHERE started < ?", (cutoff,))
    except sqlite3.Error:
        pass
    return removed


# ── Motion detection ───────────────────────────────────────────────
class MotionDetector:
    """Frame differencing against a running average.

    Kept as its own object so the detection maths can be tested offline with
    synthetic frames -- no camera, no model, no clock. update() returns
    (motion, area) where area is the summed pixel area of the moving regions that
    are individually larger than `min_area` (so sensor noise, which is scattered
    single pixels, contributes nothing).

    The running average adapts slowly while something is moving, so a person who
    pauses mid-event does not dissolve into the background and cut the clip short.
    """

    def __init__(self, cv2, np, detect_width: int = DETECT_WIDTH,
                 pixel_threshold: int = PIXEL_THRESHOLD,
                 min_area: int = MIN_AREA,
                 alpha_still: float = 0.08, alpha_moving: float = 0.02):
        self.cv2 = cv2
        self.np = np
        self.detect_width = detect_width
        self.pixel_threshold = pixel_threshold
        self.min_area = min_area
        self.alpha_still = alpha_still
        self.alpha_moving = alpha_moving
        self.background = None

    def _prepare(self, frame):
        cv2 = self.cv2
        h, w = frame.shape[:2]
        scale = self.detect_width / float(w) if w > self.detect_width else 1.0
        small = (cv2.resize(frame, (max(int(w * scale), 1), max(int(h * scale), 1)),
                            interpolation=cv2.INTER_AREA) if scale < 1.0 else frame)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(small, (21, 21), 0)

    def update(self, frame) -> tuple[bool, int]:
        cv2 = self.cv2
        gray = self._prepare(frame)
        if self.background is None:
            self.background = gray.astype("float32")
            return False, 0           # first frame only establishes the baseline
        delta = cv2.absdiff(gray, cv2.convertScaleAbs(self.background))
        thresh = cv2.threshold(delta, self.pixel_threshold, 255,
                               cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)[0]
        area = 0
        for c in contours:
            a = cv2.contourArea(c)
            if a >= self.min_area:
                area += int(a)
        motion = area >= self.min_area
        cv2.accumulateWeighted(gray, self.background,
                               self.alpha_moving if motion else self.alpha_still)
        return motion, area


# ── Camera ─────────────────────────────────────────────────────────
def _open_camera(cv2):
    """Open the webcam with the right backend for this OS. V4L2 on Linux, DirectShow
    on Windows (MSMF is slow to start), AVFoundation on macOS."""
    backends = []
    if IS_LINUX:
        backends = [getattr(cv2, "CAP_V4L2", 0)]
    elif IS_WINDOWS:
        backends = [getattr(cv2, "CAP_DSHOW", 0), getattr(cv2, "CAP_MSMF", 0)]
    else:
        backends = [getattr(cv2, "CAP_AVFOUNDATION", 0)]
    backends.append(0)   # platform default, last resort
    for be in backends:
        try:
            cap = cv2.VideoCapture(CAMERA_INDEX, be) if be else cv2.VideoCapture(CAMERA_INDEX)
        except Exception:
            continue
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            cap.release()
    return None


def latest_frame_jpeg(max_age_s: float = 5.0) -> bytes | None:
    """The newest frame the watcher has seen, as JPEG bytes. This is how the rest of
    Kamil looks through the camera while the watcher owns the device."""
    with _lock:
        if _latest_jpeg is None:
            return None
        if max_age_s and (time.time() - _latest_ts) > max_age_s:
            return None
        return _latest_jpeg


# ── The describe worker ────────────────────────────────────────────
def _describe_loop() -> None:
    """Turn keyframes into words. Runs off the capture thread so a slow inference
    never costs us frames; takes only the newest pending frame."""
    while not _stop.is_set():
        with _pending_cv:
            while _pending is None and not _stop.is_set():
                _pending_cv.wait(timeout=1.0)
            if _stop.is_set():
                return
            event_id, data = _pending            # type: ignore[misc]
            globals()["_pending"] = None

        description, faces_note = "", ""
        try:
            from core import faces as _faces
            if _faces.available():
                names = [f["name"] for f in _faces.identify_faces(data) if f.get("name")]
                if names:
                    faces_note = ", ".join(dict.fromkeys(names))
        except Exception:
            pass

        try:
            from core import vision_tier
            if vision_tier.available() and vision_tier.ensure_loaded(timeout=30):
                from models.client import LlamaClient
                client = LlamaClient(base_url=vision_tier.VISION_URL)
                description = client.describe_image(
                    data, prompt=WATCH_PROMPT, mime="image/jpeg",
                    system="You are Kamil's eyes on a room. Be factual and brief.")
                vision_tier.mark_activity()
        except Exception as e:
            description = f"(could not describe: {e})"

        if faces_note and description:
            description = f"[{faces_note}] {description}"
        elif faces_note:
            description = f"[{faces_note}] motion"

        if description:
            try:
                _event_describe(event_id, description.strip(), faces_note)
            except Exception:
                pass
            with _lock:
                _status["last_description"] = description.strip()[:400]


def _queue_description(event_id: int, jpeg: bytes) -> None:
    with _pending_cv:
        globals()["_pending"] = (event_id, jpeg)
        _pending_cv.notify()


# ── The capture loop ───────────────────────────────────────────────
def _loop() -> None:
    try:
        import cv2
        import numpy as np
    except Exception as e:
        with _lock:
            _status.update(running=False, error=f"opencv/numpy unavailable: {e}")
        return

    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    interval = 1.0 / max(FPS, 0.5)
    fourcc = cv2.VideoWriter_fourcc(*os.getenv("KAMIL_ROOM_FOURCC", "mp4v"))

    cap = None
    detector = None
    writer = None
    event_id = None
    event_started = 0.0
    last_motion = 0.0
    peak_area = 0
    clip_frames = 0
    last_vision = 0.0
    last_purge = 0.0
    backoff = 2.0

    while not _stop.is_set():
        # -- keep a camera handle, with backoff so an unplugged cam doesn't spin
        if cap is None:
            cap = _open_camera(cv2)
            if cap is None:
                with _lock:
                    _status.update(opened=False,
                                   error=f"cannot open camera {CAMERA_INDEX}")
                if _stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 2.0
            with _lock:
                _status.update(opened=True, error="")
            for _ in range(WARMUP_FRAMES):       # let auto-exposure settle
                cap.read()
            detector = None          # new handle -> new baseline

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            cap = None
            continue

        now = time.time()

        # -- publish the newest frame for `look` and the UI
        try:
            okj, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if okj:
                with _lock:
                    globals()["_latest_jpeg"] = buf.tobytes()
                    globals()["_latest_ts"] = now
        except Exception:
            pass

        # -- motion (see MotionDetector; detector is reset whenever the camera is)
        if detector is None:
            detector = MotionDetector(cv2, np)
        motion, area = detector.update(frame)

        with _lock:
            _status["frames"] += 1

        # -- event state machine
        if motion:
            last_motion = now
            peak_area = max(peak_area, area)
            if writer is None:
                stamp = datetime.now()
                name = stamp.strftime("%Y%m%d_%H%M%S") + ".mp4"
                clip_path = str(ROOM_DIR / name)
                writer = cv2.VideoWriter(clip_path, fourcc, max(FPS, 1.0),
                                         (frame.shape[1], frame.shape[0]))
                if not writer.isOpened():
                    writer.release()
                    writer = None
                    with _lock:
                        _status["error"] = "VideoWriter failed (codec missing?)"
                else:
                    event_started = now
                    clip_frames = 0
                    try:
                        event_id = _event_open(stamp, clip_path)
                    except Exception:
                        event_id = None
                    with _lock:
                        _status.update(recording=True, events=_status["events"] + 1,
                                       last_event_id=event_id,
                                       last_motion=stamp.isoformat(timespec="seconds"))
                    # one description per cooldown, on the frame that opened the event
                    if event_id is not None and (now - last_vision) >= VISION_COOLDOWN_S:
                        last_vision = now
                        key = latest_frame_jpeg(max_age_s=0)
                        if key:
                            _queue_description(event_id, key)
            else:
                with _lock:
                    _status["last_motion"] = datetime.now().isoformat(timespec="seconds")

        if writer is not None:
            try:
                writer.write(frame)
                clip_frames += 1
            except Exception:
                pass
            stale = (now - last_motion) > CLIP_TAIL_S
            too_long = (now - event_started) > MAX_CLIP_S
            if stale or too_long:
                duration = now - event_started
                writer.release()
                writer = None
                if event_id is not None:
                    if duration >= MIN_EVENT_S:
                        try:
                            _event_close(event_id, datetime.now(), duration,
                                         peak_area, clip_frames)
                        except Exception:
                            pass
                    else:
                        _discard_event(event_id)
                event_id = None
                peak_area = 0
                with _lock:
                    _status["recording"] = False

        # -- housekeeping once an hour
        if (now - last_purge) > 3600:
            last_purge = now
            try:
                _purge_old()
            except Exception:
                pass

        if _stop.wait(interval):
            break

    if writer is not None:
        writer.release()
    if cap is not None:
        cap.release()
    with _lock:
        _status.update(running=False, recording=False, opened=False)


def _discard_event(event_id: int) -> None:
    """Drop a blip too short to be worth keeping, clip and all."""
    try:
        with _db() as conn:
            row = conn.execute("SELECT clip_path FROM room_events WHERE id=?",
                               (event_id,)).fetchone()
            if row and row[0] and os.path.isfile(row[0]):
                try:
                    os.remove(row[0])
                except OSError:
                    pass
            conn.execute("DELETE FROM room_events WHERE id=?", (event_id,))
    except sqlite3.Error:
        pass


# ── Control surface ────────────────────────────────────────────────
def start(force: bool = False) -> bool:
    """Start watching. Returns True when the threads are up. Idempotent."""
    global _thread, _describe_thread
    if not (ENABLED or force):
        with _lock:
            _status["error"] = "room watch disabled (set KAMIL_ROOM_WATCH=1)"
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
    _stop.clear()
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    _thread = threading.Thread(target=_loop, name="room-watch", daemon=True)
    _describe_thread = threading.Thread(target=_describe_loop,
                                        name="room-describe", daemon=True)
    with _lock:
        _status.update(running=True, error="",
                       started_at=datetime.now().isoformat(timespec="seconds"))
    _thread.start()
    _describe_thread.start()
    return True


def stop(timeout: float = 5.0) -> None:
    """Stop watching and release the camera."""
    _stop.set()
    with _pending_cv:
        _pending_cv.notify_all()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    with _lock:
        _status.update(running=False, recording=False)


def is_running() -> bool:
    with _lock:
        return bool(_status["running"]) and _thread is not None and _thread.is_alive()


def status() -> dict:
    """Live state for the UI and /room endpoints."""
    with _lock:
        s = dict(_status)
    s["enabled"] = ENABLED
    s["alive"] = _thread is not None and _thread.is_alive()
    s["has_frame"] = _latest_jpeg is not None
    s["clip_dir"] = str(ROOM_DIR)
    s["retention_days"] = RETENTION_DAYS
    try:
        s["clips_on_disk"] = len(list(ROOM_DIR.glob("*.mp4")))
        s["disk_mb"] = round(
            sum(p.stat().st_size for p in ROOM_DIR.glob("*.mp4")) / 1e6, 1)
    except OSError:
        pass
    return s
