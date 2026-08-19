"""
chatterbox_server.py — a tiny local TTS microservice for Chatterbox.

Run with the ISOLATED tts_env Python (its deps — torch 2.6 CUDA, transformers 5.2,
diffusers, gradio — would shatter Kamil's main venv, so Chatterbox lives here and
Kamil talks to it over HTTP). Kamil's backend POSTs a sentence, gets back WAV.

  tts_env\\Scripts\\python.exe chatterbox_server.py     # port 8095

100% local: the model runs on this machine's GPU; nothing leaves.
"""
import io
import json
import os
import sys

PORT = int(os.getenv("KAMIL_CHATTERBOX_PORT", "8095"))

import torch
import numpy as np
import soundfile as sf
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# perth's real (onnxruntime-based) watermarker isn't installed in this env, so its
# class is None and model load crashes. We don't need the inaudible AI-watermark for
# a private local assistant — use perth's no-op DummyWatermarker instead.
import perth
if getattr(perth, "PerthImplicitWatermarker", None) is None:
    perth.PerthImplicitWatermarker = perth.DummyWatermarker

from chatterbox.tts import ChatterboxTTS

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
REF = os.getenv("KAMIL_CHATTERBOX_REF")   # optional reference wav → clone that voice
EXAG = float(os.getenv("KAMIL_CHATTERBOX_EXAG", "0.5"))
CFG = float(os.getenv("KAMIL_CHATTERBOX_CFG", "0.5"))

print(f"[chatterbox] loading model on {DEVICE}...", flush=True)
MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
SR = int(MODEL.sr)

# Prime the CUDA graphs so the FIRST real reply isn't the ~2.8s cold generation.
try:
    with torch.inference_mode():
        MODEL.generate("Ready.", exaggeration=EXAG, cfg_weight=CFG)
    print("[chatterbox] warmup done", flush=True)
except Exception as _e:
    print(f"[chatterbox] warmup skipped: {_e}", flush=True)

print(f"[chatterbox] ready on :{PORT} (sr={SR}, device={DEVICE}, ref={'yes' if REF else 'no'})", flush=True)


def synth(text: str, exaggeration: float, cfg_weight: float) -> bytes:
    kw = {}
    if REF and os.path.isfile(REF):
        kw["audio_prompt_path"] = REF
    with torch.inference_mode():
        wav = MODEL.generate(text, exaggeration=exaggeration, cfg_weight=cfg_weight, **kw)
    arr = wav.squeeze(0).detach().cpu().numpy().astype("float32")
    buf = io.BytesIO()
    sf.write(buf, arr, SR, format="WAV")
    buf.seek(0)
    return buf.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/tts":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            text = (data.get("text") or "").strip()
            if not text:
                self.send_response(400); self.end_headers(); return
            wav = synth(text, float(data.get("exaggeration", EXAG)), float(data.get("cfg_weight", CFG)))
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        except Exception as e:
            msg = str(e).encode()
            self.send_response(500); self.send_header("Content-Length", str(len(msg))); self.end_headers()
            self.wfile.write(msg)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
