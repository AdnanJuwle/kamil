"""Tests for core/model.py — the single-model resolver.

The riskiest thing this module does is pair a multimodal projector with the
weights. Projectors are NOT interchangeable: an mmproj built for Qwen3-VL-8B
produces embeddings a 27B model cannot read, and llama-server will either refuse
to load or emit nonsense. An early version of this resolver happily paired an 8B
projector with a 30B coder model and reported "vision available", so most of these
tests are about refusing to do that.
"""
import importlib

import pytest

from core import model as model_mod


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """An empty models/ directory that core.model resolves against."""
    d = tmp_path / "models"
    d.mkdir()
    monkeypatch.setenv("KAMIL_MODELS_DIR", str(d))
    for var in ("KAMIL_MODEL", "KAMIL_MMPROJ", "KAMIL_PRIMARY_MULTIMODAL",
                "KAMIL_MODEL_HINT"):
        monkeypatch.delenv(var, raising=False)
    from core import paths
    importlib.reload(paths)
    m = importlib.reload(model_mod)
    yield d, m
    importlib.reload(paths)
    importlib.reload(model_mod)


def put(d, name, size_mb=1):
    f = d / name
    f.write_bytes(b"\0" * (size_mb * 1024))
    return f


# ── projector / weights pairing ────────────────────────────────────

@pytest.mark.parametrize("proj,weights,expected", [
    # huihui-ai ships a generically-named projector: it belongs to whatever single
    # model is being served, so pair it.
    ("mmproj-model-bf16.gguf", "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", True),
    ("mmproj-model-f16.gguf", "Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf", True),
    # a family-named projector must agree on BOTH family and size
    ("mmproj-Qwen3-VL-8B-Instruct-F16.gguf", "Qwen3-VL-8B-Instruct-Q8_0.gguf", True),
    # 8B projector, 27B weights -> would silently garble every frame
    ("mmproj-Qwen3-VL-8B-Instruct-F16.gguf",
     "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", False),
    # 8B projector, 30B coder -> wrong family AND wrong size
    ("mmproj-Qwen3-VL-8B-Instruct-F16.gguf",
     "qwen3-coder-30b-a3b-instruct-q4_k_m.gguf", False),
    # different family entirely
    ("mmproj-gemma-3-27b-F16.gguf", "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", False),
])
def test_projector_matching(proj, weights, expected):
    assert model_mod._projector_matches(proj, weights) is expected


def test_mismatched_projector_means_text_only(models_dir):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    put(d, "mmproj-Qwen3-VL-8B-Instruct-F16.gguf", 10)
    assert m.mmproj_path() is None          # better text-only than garbled
    assert m.is_multimodal() is False
    assert "projector" in m.missing_reason()


def test_generic_projector_enables_vision(models_dir):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    proj = put(d, "mmproj-model-bf16.gguf", 10)
    assert m.mmproj_path() == str(proj)
    assert m.is_multimodal() is True
    assert m.missing_reason() == ""
    assert "vision" in m.label()


def test_mmproj_is_never_chosen_as_the_weights(models_dir):
    d, m = models_dir
    put(d, "mmproj-model-bf16.gguf", 10)
    assert m.model_path() is None           # a projector alone is not a model
    assert "no usable GGUF" in m.missing_reason()


# ── weights selection ─────────────────────────────────────────────

def test_prefers_the_huihui_model_over_others(models_dir):
    d, m = models_dir
    put(d, "llama-3.1-8b-lexi-uncensored-q4.gguf", 400)
    put(d, "gemma4-e4b-q4.gguf", 200)
    want = put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    assert m.model_path() == str(want)


def test_explicit_model_env_wins(models_dir, monkeypatch):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    other = put(d, "gemma4-e4b-q4.gguf", 100)
    monkeypatch.setenv("KAMIL_MODEL", str(other))
    assert m.model_path() == str(other)


def test_missing_model_env_does_not_silently_fall_back(models_dir, monkeypatch):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    monkeypatch.setenv("KAMIL_MODEL", str(d / "does-not-exist.gguf"))
    assert m.model_path() is None


def test_ternary_variants_are_avoided(models_dir):
    """The card says stock llama.cpp cannot run these -- never pick one by default."""
    d, m = models_dir
    normal = put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    put(d, "Huihui-Qwen3.8-27B-abliterated-Ternary-Bonsai-f16.gguf", 900)
    assert m.model_path() == str(normal)


def test_split_gguf_resolves_to_first_shard(models_dir):
    d, m = models_dir
    first = put(d, "Huihui-Qwen3.8-27B-abliterated-Q8_0-00001-of-00002.gguf", 500)
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q8_0-00002-of-00002.gguf", 500)
    assert m.model_path() == str(first)


def test_empty_models_dir_is_reported_not_crashed(models_dir):
    _d, m = models_dir
    assert m.model_path() is None
    assert m.is_multimodal() is False
    assert "Huihui-Qwen3.8-27B-abliterated-GGUF" in m.missing_reason()
    with pytest.raises(RuntimeError):
        m.serve_args()


# ── serve args ────────────────────────────────────────────────────

def test_serve_args_are_complete_and_loopback_only(models_dir):
    d, m = models_dir
    weights = put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    proj = put(d, "mmproj-model-bf16.gguf", 10)
    args = m.serve_args(port=8080)
    joined = " ".join(args)
    assert "-m" in args and str(weights) in args
    assert "--mmproj" in args and str(proj) in args
    # Kamil's trust boundary is loopback; the old Electron shell served 0.0.0.0
    assert "127.0.0.1" in args
    assert "0.0.0.0" not in joined
    assert "--port" in args and "8080" in args
    assert "--jinja" in args            # native tool-calling


def test_serve_args_omit_mmproj_when_text_only(models_dir):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    assert "--mmproj" not in m.serve_args()


def test_multimodal_can_be_forced_off(models_dir, monkeypatch):
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    put(d, "mmproj-model-bf16.gguf", 10)
    monkeypatch.setenv("KAMIL_PRIMARY_MULTIMODAL", "0")
    assert m.is_multimodal() is False


def test_describe_is_json_safe(models_dir):
    import json
    d, m = models_dir
    put(d, "Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf", 500)
    put(d, "mmproj-model-bf16.gguf", 10)
    json.dumps(m.describe(), default=str)   # the /model endpoint returns this
