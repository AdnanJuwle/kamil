"""
core/hwfit.py — compute llama.cpp serve flags to fit a model on this machine.

Turns "how do I run a model bigger than my VRAM" into a formula. Given the GPU's
VRAM (and RAM as overflow), a model's size, and whether it's a Mixture-of-Experts,
it returns concrete llama.cpp flags — n_gpu_layers, n_cpu_moe, KV-cache type, ctx —
so the model loads and runs.

The offload mechanics (llama.cpp):
  - MoE model too big for VRAM: --n-cpu-moe N keeps the expert weights of the first
    N layers in CPU/RAM while the ~active path + attention stay on the GPU. Since a
    MoE only fires a few experts per token, this is the cheap way to run a big model
    (e.g. a 35B-A3B) with most speed intact.
  - Dense model too big: -ngl <N> puts N layers on the GPU, the rest spill to RAM
    (slower — dense has no per-expert knob).

Pure/deterministic (adapted from Odysseus's hwfit). 100% local — this only decides
flags for a local llama-server; nothing leaves the machine.
"""

import math
import os
import re
import subprocess

# GB per billion params, by quant (weights = params_b * bpp).
QUANT_BPP = {
    "Q2_K": 0.36, "Q3_K_M": 0.46, "Q4_K_M": 0.58, "Q4_0": 0.56,
    "Q5_K_M": 0.68, "Q6_K": 0.82, "Q8_0": 1.06, "F16": 2.0,
}
# KV-cache cost scales with cache type (q4_0 ~= half of q8_0 ~= half of f16).
KV_FACTOR = {"q4_0": 0.5, "q8_0": 1.0, "f16": 2.0}

RUNTIME_BUFFER_GB = 0.8   # compute/graph buffers
DESKTOP_RESERVE_GB = 1.0  # leave headroom for Windows desktop / other VRAM users


def detect_vram_gb() -> float:
    """Total GPU VRAM in GB. Env override wins; else nvidia-smi; else 24 (3090)."""
    env = os.getenv("KAMIL_VRAM_GB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        if out:
            return round(float(out[0].strip()) / 1024, 1)
    except Exception:
        pass
    return 24.0


def _params_b_from_name(name: str) -> float:
    """Best-effort billions-of-params from a model filename (e.g. '35b', '8b')."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', name.lower())
    return float(m.group(1)) if m else 8.0


def _active_b_from_name(name: str, params_b: float) -> float:
    """Active params for a MoE (e.g. 'a3b' -> 3B active); else all params."""
    m = re.search(r'a(\d+(?:\.\d+)?)b', name.lower())
    return float(m.group(1)) if m else params_b


def model_meta(path: str) -> dict:
    """Infer serving-relevant metadata from a GGUF path (size on disk + name)."""
    name = os.path.basename(path)
    try:
        weights_gb = os.path.getsize(path) / 1073741824
    except OSError:
        weights_gb = 0.0
    params_b = _params_b_from_name(name)
    is_moe = bool(re.search(r'a\d+(?:\.\d+)?b|moe', name.lower()))
    active_b = _active_b_from_name(name, params_b) if is_moe else params_b
    # layer-count heuristic by size (28-64 for most LLMs)
    n_layers = 64 if params_b >= 60 else 48 if params_b >= 25 else 40 if params_b >= 12 else 32
    return {"name": name, "weights_gb": round(weights_gb, 2), "params_b": params_b,
            "is_moe": is_moe, "active_b": active_b, "n_layers": n_layers}


def _kv_gb(active_b: float, ctx: int, kv_type: str) -> float:
    return 0.000008 * active_b * ctx * KV_FACTOR.get(kv_type, 1.0)


def fit_profile(meta: dict, vram_gb: float = None, ctx: int = 8192,
                kv_type: str = "q8_0") -> dict:
    """Compute serve flags so `meta`'s model fits `vram_gb`. Returns a dict with
    n_gpu_layers, n_cpu_moe, ctx, cache_type, est_vram_gb, fully_gpu, note."""
    if vram_gb is None:
        vram_gb = detect_vram_gb()
    budget = vram_gb - DESKTOP_RESERVE_GB
    weights = meta.get("weights_gb", 0.0)
    n_layers = max(meta.get("n_layers", 32), 1)
    kv = _kv_gb(meta.get("active_b", meta.get("params_b", 8)), ctx, kv_type)
    needed = weights + kv + RUNTIME_BUFFER_GB

    if needed <= budget:
        return {"n_gpu_layers": 999, "n_cpu_moe": 0, "ctx": ctx, "cache_type": kv_type,
                "est_vram_gb": round(needed, 1), "fully_gpu": True,
                "note": "fits fully on GPU"}

    per_layer = weights / n_layers
    overflow = needed - budget

    if meta.get("is_moe"):
        n = min(n_layers, math.ceil(overflow / max(per_layer, 1e-6)))
        est = weights - n * per_layer + kv + RUNTIME_BUFFER_GB
        return {"n_gpu_layers": 999, "n_cpu_moe": n, "ctx": ctx, "cache_type": kv_type,
                "est_vram_gb": round(est, 1), "fully_gpu": False,
                "note": f"MoE: {n}/{n_layers} expert layers on CPU/RAM (most speed intact)"}

    # dense: put as many layers on GPU as fit, rest spill to RAM
    avail = budget - kv - RUNTIME_BUFFER_GB
    n_gpu = max(0, int(avail / max(per_layer, 1e-6)))
    return {"n_gpu_layers": n_gpu, "n_cpu_moe": 0, "ctx": ctx, "cache_type": kv_type,
            "est_vram_gb": round(n_gpu * per_layer + kv + RUNTIME_BUFFER_GB, 1),
            "fully_gpu": False,
            "note": f"dense: {n_gpu}/{n_layers} layers on GPU, rest on RAM (slower)"}


def serve_args(model_path: str, profile: dict, port: int) -> list[str]:
    """Build the llama-server argument list from a fit profile."""
    args = ["-m", model_path,
            "-c", str(profile.get("ctx", 8192)),
            "-ngl", str(profile.get("n_gpu_layers", 999)),
            "--flash-attn", "on",
            "--host", "127.0.0.1", "--port", str(port), "--log-disable"]
    if os.getenv("KAMIL_JINJA", "1") == "1":
        args.append("--jinja")   # model's chat template -> enables native tool-calling
    if profile.get("n_cpu_moe", 0) > 0:
        args += ["--n-cpu-moe", str(profile["n_cpu_moe"])]
    ct = profile.get("cache_type")
    if ct and ct != "f16":
        args += ["--cache-type-k", ct, "--cache-type-v", ct]
    return args
