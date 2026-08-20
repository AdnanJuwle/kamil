"""
core/faces.py — local face recognition to complement voice speaker-ID.

Kamil already hears who's talking (resemblyzer); this lets him SEE who's in front of
the camera. InsightFace (buffalo_l: RetinaFace detect + ArcFace embed) runs on
onnxruntime-GPU — same stack as Kokoro/SNAC, NO torch-CUDA, 100% local. Enrolled
faces are stored as embedding vectors in data/faces/<name>.npy (no images kept).

Adapted from Odysseus's faces service (detection + embedding).
"""
from __future__ import annotations

import os
import threading

import numpy as np

from core.config import DATA_DIR

FACES_DIR = os.path.join(DATA_DIR, "faces")
MATCH_THRESHOLD = float(os.getenv("KAMIL_FACE_THRESHOLD", "0.42"))   # ArcFace cosine

_app = None
_lock = threading.Lock()


def _get_app():
    """Lazy-load InsightFace on the GPU (onnxruntime CUDA), staging cuDNN DLLs like
    the other onnx models. Downloads buffalo_l (~300MB) once on first use."""
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is None:
            import glob
            import onnxruntime as ort
            try:
                nvidia_root = os.path.abspath(os.path.join(os.path.dirname(ort.__file__), "..", "nvidia"))
                for b in [p for p in glob.glob(os.path.join(nvidia_root, "*", "bin")) if os.path.isdir(p)]:
                    os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
                    try:
                        os.add_dll_directory(b)
                    except Exception:
                        pass
            except Exception:
                pass
            from insightface.app import FaceAnalysis
            provs = ort.get_available_providers()
            use = (["CUDAExecutionProvider"] if "CUDAExecutionProvider" in provs else []) + ["CPUExecutionProvider"]
            app = FaceAnalysis(name="buffalo_l", providers=use)
            app.prepare(ctx_id=0, det_size=(640, 640))
            _app = app
            print(f"[faces] InsightFace ready ({app.models and 'buffalo_l'})")
    return _app


def _decode(image_bytes: bytes):
    import cv2
    arr = np.frombuffer(image_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _known() -> dict[str, np.ndarray]:
    out = {}
    if os.path.isdir(FACES_DIR):
        for f in os.listdir(FACES_DIR):
            if f.endswith(".npy"):
                try:
                    out[f[:-4]] = np.load(os.path.join(FACES_DIR, f))
                except Exception:
                    pass
    return out


def enroll_face(name: str, image_bytes: bytes) -> dict:
    """Enroll the largest face in the image under `name` (stores only the embedding)."""
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "need a name"}
    img = _decode(image_bytes)
    if img is None:
        return {"success": False, "error": "couldn't read the image"}
    faces = _get_app().get(img)
    if not faces:
        return {"success": False, "error": "no face detected — look at the camera in good light"}
    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    os.makedirs(FACES_DIR, exist_ok=True)
    np.save(os.path.join(FACES_DIR, f"{name}.npy"), face.normed_embedding.astype(np.float32))
    return {"success": True, "name": name}


def identify_faces(image_bytes: bytes) -> list[dict]:
    """Detect faces in the image and match against enrolled ones.
    Returns [{name|None, score}] largest-first."""
    img = _decode(image_bytes)
    if img is None:
        return []
    faces = _get_app().get(img)
    known = _known()
    out = []
    for f in sorted(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True):
        emb = f.normed_embedding
        best_name, best = None, 0.0
        for name, kemb in known.items():
            score = float(np.dot(emb, kemb))   # both normed -> cosine similarity
            if score > best:
                best_name, best = name, score
        out.append({"name": best_name if best >= MATCH_THRESHOLD else None, "score": round(best, 3)})
    return out


def list_faces() -> list[str]:
    return sorted(_known().keys())


def available() -> bool:
    try:
        import insightface  # noqa: F401
        return True
    except Exception:
        return False
