"""
core/orpheus_tts.py — turn Orpheus audio tokens into a 24kHz waveform (SNAC).

Orpheus (served by core/tts_tier.py on llama-server) emits tokens that detokenise to
`<custom_token_N>`. Seven of them make one SNAC frame; the SNAC codec decodes frames
into audio. This module formats the prompt, streams tokens from llama-server, and
decodes them to float32 PCM chunks as they arrive — so speech starts within a few
hundred ms instead of after the whole sentence.

Decoder math + prompt format follow the canopyai/Orpheus-TTS reference exactly.
100% local: llama-server for the tokens, SNAC (torch) for the waveform.
"""
from __future__ import annotations

import json
import os
import re
import threading

import httpx
import numpy as np

from core import tts_tier
from core.config import MODELS_DIR

SAMPLE_RATE = 24000
DEFAULT_VOICE = os.getenv("KAMIL_ORPHEUS_VOICE", "tara")   # tara/leah/jess/leo/dan/mia/zac/zoe
SNAC_ONNX = os.getenv("KAMIL_SNAC_ONNX", os.path.join(MODELS_DIR, "snac_decoder_fp16.onnx"))

# Prompt = token ids: [128259] + tokens("{voice}: {text}") + [128009,128260,128261,128257]
# (canopyai reference — this conditions the model on YOUR text; the <|audio|> string
# format made the model speak its own random sentence instead).
_START = [128259]
_END = [128009, 128260, 128261, 128257]


def _prompt_ids(voice: str, text: str) -> list[int]:
    r = httpx.post(f"{tts_tier.TTS_URL}/tokenize",
                   json={"content": f"{voice}: {text}", "add_special": False}, timeout=15)
    r.raise_for_status()
    return _START + list(r.json().get("tokens", [])) + _END

_snac = None
_snac_lock = threading.Lock()
_CUSTOM_RE = re.compile(r"<custom_token_(\d+)>")


def _get_snac():
    """Lazy-load the SNAC decoder as an onnxruntime session on the GPU (CUDA) — no
    torch. Falls back to CPU if CUDA isn't available. This is what keeps synthesis
    real-time (torch SNAC on CPU was the bottleneck)."""
    global _snac
    if _snac is not None:
        return _snac
    with _snac_lock:
        if _snac is None:
            import glob
            import onnxruntime as ort
            # stage the cuDNN/cuBLAS wheel DLLs on PATH so ORT's CUDA provider loads
            # (same trick Kokoro uses; harmless/idempotent if already staged)
            try:
                nvidia_root = os.path.abspath(os.path.join(os.path.dirname(ort.__file__), "..", "nvidia"))
                bins = [p for p in glob.glob(os.path.join(nvidia_root, "*", "bin")) if os.path.isdir(p)]
                if bins:
                    os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
                    for b in bins:
                        try:
                            os.add_dll_directory(b)
                        except Exception:
                            pass
            except Exception:
                pass
            provs = ort.get_available_providers()
            use = (["CUDAExecutionProvider"] if "CUDAExecutionProvider" in provs else []) + ["CPUExecutionProvider"]
            _snac = ort.InferenceSession(SNAC_ONNX, providers=use)
            print(f"[orpheus] SNAC decoder on {_snac.get_providers()[0]}")
    return _snac


def _turn_token_into_id(number: int, index: int) -> int:
    return number - 10 - ((index % 7) * 4096)


def _decode_codes(codes_list: list[int]):
    """Decode a full run of SNAC codes (len a multiple of 7) → float32 PCM via the
    onnxruntime SNAC decoder. No edge trimming (clean word onsets/offsets); invalid
    codes are clamped, not dropped."""
    n = len(codes_list) // 7
    if n == 0:
        return None
    frame = codes_list[:n * 7]
    c0, c1, c2 = [], [], []
    for j in range(n):
        i = 7 * j
        c0.append(frame[i])
        c1.append(frame[i + 1]); c1.append(frame[i + 4])
        c2.append(frame[i + 2]); c2.append(frame[i + 3]); c2.append(frame[i + 5]); c2.append(frame[i + 6])
    a0 = np.clip(np.array([c0], dtype=np.int64), 0, 4095)
    a1 = np.clip(np.array([c1], dtype=np.int64), 0, 4095)
    a2 = np.clip(np.array([c2], dtype=np.int64), 0, 4095)
    out = _get_snac().run(["audio_values"],
                          {"audio_codes.0": a0, "audio_codes.1": a1, "audio_codes.2": a2})[0]
    return _trim_silence(out[0, 0].astype(np.float32))


def _trim_silence(audio: np.ndarray, thresh: float = 0.01, pad_ms: int = 40) -> np.ndarray:
    """Trim Orpheus's leading/trailing near-silence so speech is tight (lower latency,
    no dead air between clauses). Keeps a small pad so word edges aren't clipped."""
    if audio.size == 0:
        return audio
    loud = np.abs(audio) > thresh
    if not loud.any():
        return audio[:1]
    pad = int(SAMPLE_RATE * pad_ms / 1000)
    a = max(0, int(np.argmax(loud)) - pad)
    b = min(len(audio), len(audio) - int(np.argmax(loud[::-1])) + pad)
    return audio[a:b]


def _token_worker(text: str, voice: str, out: "queue.Queue"):
    """Stream tokens from llama-server for one clause, collect the SNAC codes, then
    decode the WHOLE clause in one shot → clean audio. (Clauses are short — see the
    caller — so first-audio latency stays low.)"""
    try:
        _get_snac()  # ensure codec ready before we time audio
        body = {"prompt": _prompt_ids(voice, text),
                "n_predict": int(os.getenv("KAMIL_ORPHEUS_MAXTOK", "4096")),
                "temperature": 0.6, "top_p": 0.9, "repeat_penalty": 1.1,
                "stream": True, "cache_prompt": False}
        codes: list[int] = []
        count = 0
        with httpx.stream("POST", f"{tts_tier.TTS_URL}/completion", json=body, timeout=120) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                for m in _CUSTOM_RE.finditer(ev.get("content", "")):
                    code = _turn_token_into_id(int(m.group(1)), count)
                    count += 1
                    if code is not None and code >= 0:
                        codes.append(code)
                if ev.get("stop"):
                    break
        pcm = _decode_codes(codes)
        if pcm is not None and len(pcm):
            out.put(pcm)
    except Exception as e:
        out.put(("__err__", str(e)))
    finally:
        out.put(None)


async def stream(text: str, voice: str | None = None):
    """Async generator: yields float32 PCM chunks (24kHz) as Orpheus speaks `text`.
    Runs token-fetch + SNAC decode in a thread so the event loop stays free."""
    import asyncio
    import queue as _queue
    voice = voice or DEFAULT_VOICE
    q: _queue.Queue = _queue.Queue()
    threading.Thread(target=_token_worker, args=(text, voice, q), daemon=True).start()
    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is None:
            break
        if isinstance(item, tuple) and item and item[0] == "__err__":
            print(f"[orpheus] error: {item[1]}")
            break
        yield item


def warm() -> None:
    """Best-effort: preload SNAC so the first spoken reply isn't paying that cost."""
    try:
        _get_snac()
    except Exception as e:
        print(f"[orpheus] SNAC warm failed: {e}")
