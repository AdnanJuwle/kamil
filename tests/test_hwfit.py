"""Tests for core/hwfit.py — the model fit / RAM-offload engine."""
from core import hwfit


def test_small_model_fits_fully():
    meta = {"name": "x-8b.gguf", "weights_gb": 4.6, "params_b": 8,
            "is_moe": False, "active_b": 8, "n_layers": 32}
    p = hwfit.fit_profile(meta, vram_gb=24.0)
    assert p["fully_gpu"] is True
    assert p["n_gpu_layers"] == 999 and p["n_cpu_moe"] == 0


def test_moe_overflow_offloads_experts():
    meta = {"name": "x-35b-a3b.gguf", "weights_gb": 20.8, "params_b": 35,
            "is_moe": True, "active_b": 3, "n_layers": 48}
    p = hwfit.fit_profile(meta, vram_gb=17.0)   # chat model already using VRAM
    assert p["fully_gpu"] is False
    assert p["n_cpu_moe"] > 0 and p["n_gpu_layers"] == 999
    assert p["est_vram_gb"] <= 17.0            # actually fits the budget


def test_dense_overflow_reduces_gpu_layers():
    meta = {"name": "x-70b.gguf", "weights_gb": 40.0, "params_b": 70,
            "is_moe": False, "active_b": 70, "n_layers": 80}
    p = hwfit.fit_profile(meta, vram_gb=24.0)
    assert p["n_cpu_moe"] == 0
    assert 0 < p["n_gpu_layers"] < 80          # partial GPU, rest spills to RAM


def test_model_meta_parses_name():
    m = hwfit.model_meta("models/qwen3.6-35b-a3b-q4.gguf")
    assert m["params_b"] == 35 and m["is_moe"] is True and m["active_b"] == 3
    m2 = hwfit.model_meta("models/llama-3.1-8b-instruct-q4.gguf")
    assert m2["params_b"] == 8 and m2["is_moe"] is False


def test_serve_args_includes_offload_flags():
    prof = {"n_gpu_layers": 999, "n_cpu_moe": 14, "ctx": 8192, "cache_type": "q8_0"}
    args = hwfit.serve_args("m.gguf", prof, 8086)
    assert "--n-cpu-moe" in args and "14" in args
    assert "--cache-type-k" in args and "q8_0" in args
    assert args[args.index("--port") + 1] == "8086"


def test_serve_args_no_moe_flag_when_zero():
    prof = {"n_gpu_layers": 999, "n_cpu_moe": 0, "ctx": 4096, "cache_type": "f16"}
    args = hwfit.serve_args("m.gguf", prof, 8080)
    assert "--n-cpu-moe" not in args
    assert "--cache-type-k" not in args        # f16 = default, not passed


def test_detect_vram_env_override(monkeypatch):
    monkeypatch.setenv("KAMIL_VRAM_GB", "16")
    assert hwfit.detect_vram_gb() == 16.0
