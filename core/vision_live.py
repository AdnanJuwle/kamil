"""
core/vision_live.py — what Kamil is looking at RIGHT NOW.

A tiny thread-safe holder for the current vision event so the UI can show a live
thumbnail of the exact frame/image Kamil is analysing (in the presence column, not
buried in background history). Seeing is a LIVE act: set_seeing() when a frame goes
to the vision model, set_result() when the answer comes back.

Only a small JPEG thumbnail is kept in memory — never the full frame, never on disk.
"""
from __future__ import annotations

import base64
import threading
import time

_lock = threading.Lock()
_state = {
    "active": False, "status": "idle", "thumb": "", "question": "",
    "source": "", "answer": "", "ts": 0.0, "id": 0,
}


def make_thumb(image_bytes: bytes, mime: str = "image/jpeg", max_px: int = 180) -> str:
    """Downscale to a small JPEG data-URI for the live preview. Uses cv2 when present
    (tiny ~5KB thumb); otherwise falls back to the original bytes as a data-URI."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            s = max_px / max(h, w)
            if s < 1:
                img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        pass
    return f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")


def set_seeing(thumb: str, question: str, source: str = "") -> None:
    """A frame is being analysed now. `source` is e.g. 'webcam' or an image basename."""
    with _lock:
        _state.update(active=True, status="seeing", thumb=thumb, question=question,
                      source=source, answer="", ts=time.time(), id=_state["id"] + 1)


def set_result(answer: str) -> None:
    with _lock:
        _state.update(active=False, status="done", answer=str(answer)[:2000], ts=time.time())


def clear() -> None:
    with _lock:
        _state.update(active=False, status="idle", thumb="", question="", answer="")


def current() -> dict:
    with _lock:
        return dict(_state)
