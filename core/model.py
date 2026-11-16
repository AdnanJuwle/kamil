"""
core/model.py -- the one model.

Kamil used to run two LLMs: a primary on :8080 for reasoning and a small voice
model on :8084 for fast spoken replies, with a "let me check that for you" handoff
between them. That split existed because the voice model was a 4B -- fast but too
dim to answer anything real.

Huihui-Qwen3.8-27B-abliterated replaces both. It is one model that is:
  * strong enough to answer directly, so the voice handoff is dead weight
  * multimodal (image-text-to-text) via llama.cpp's mtmd, so the SAME server that
    holds the conversation can also look through the webcam -- no second process,
    no VRAM split, no model swap latency on a motion event

So this module is the single place that answers "which weights, which projector,
and what flags does llama-server need to serve them here". The launchers
(start.sh, start.bat, the Electron shell) all ask Python rather than hard-coding a
path, which is what makes the same tree boot on Fedora and Windows.

Resolution order for the weights:
  1. KAMIL_MODEL -- an explicit path, wins outright
  2. the best GGUF in models/ matching KAMIL_MODEL_HINT (default: the Huihui 27B)
  3. the best general-purpose GGUF present, so a fresh clone still boots

100% local. Nothing here downloads anything or touches the network.
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

from core.paths import MODELS_DIR, llama_server_bin  # noqa: F401

# The model Kamil is built around. A hint, not a hard filename -- quant choice is
# the user's (Q4_K ~16.8GB, Q6_K ~22.4GB, Q8_0 ~29GB) and the file they downloaded
# may carry any of the UD-/GSQ- infixes huihui-ai publishes.
MODEL_HINT = os.getenv("KAMIL_MODEL_HINT", "huihui-qwen3.8-27b")

# Context window. The card advertises very long contexts with yarn scaling, but
# KV cache at 262144 does not fit alongside 27B of weights on a consumer card.
MODEL_CTX = int(os.getenv("KAMIL_CTX", "16384"))

PRIMARY_PORT = int(os.getenv("KAMIL_PRIMARY_PORT", "8080"))


def _is_mmproj(name: str) -> bool:
    """A multimodal projector (the image encoder), not a set of LLM weights."""
    low = os.path.basename(name).lower()
    return "mmproj" in low or "mproj" in low


def _all_ggufs() -> list[str]:
    """Every GGUF in models/, excluding later shards of a split file (llama.cpp is
    given shard 1 and finds the rest itself)."""
    out = []
    for p in glob.glob(os.path.join(str(MODELS_DIR), "*.gguf")):
        base = os.path.basename(p)
        m = re.search(r'-(\d+)-of-\d+\.gguf$', base, re.I)
        if m and m.group(1) != "00001" and m.group(1) != "1":
            continue
        out.append(p)
    return out


def _hint_tokens() -> list[str]:
    """The hint split into matchable pieces: 'huihui-qwen3.8-27b' -> huihui, qwen3.8, 27b."""
    return [t for t in re.split(r'[-_\s.]+(?![0-9])|[-_\s]+', MODEL_HINT.lower()) if t]


def _score(path: str) -> int:
    """How well a GGUF matches what Kamil wants. Higher is better."""
    name = os.path.basename(path).lower()
    if _is_mmproj(name):
        return -1000
    score = 0
    for tok in _hint_tokens():
        if tok and tok in name:
            score += 40
    # family fallbacks, so a near-miss download still gets picked over a 7B
    if "huihui" in name:
        score += 25
    if "abliterated" in name or "uncensored" in name:
        score += 10
    if "qwen3" in name:
        score += 8
    # prefer instruct-shaped weights over base
    if "instruct" in name or "-it-" in name:
        score += 4
    # bigger is better, up to a point
    m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', name)
    if m:
        try:
            score += min(int(float(m.group(1))), 70) // 4
        except ValueError:
            pass
    # these need a patched llama.cpp ("Stock llama.cpp will not run these files")
    if "ternary" in name or "bonsai" in name:
        score -= 200
    return score


def model_path() -> str | None:
    """The weights Kamil serves. None when models/ holds no usable GGUF."""
    env = os.getenv("KAMIL_MODEL")
    if env:
        p = Path(env).expanduser()
        return str(p) if p.is_file() else None
    cands = [p for p in _all_ggufs() if not _is_mmproj(p)]
    if not cands:
        return None
    return max(cands, key=_score)


# Tokens that say nothing about WHICH model a file belongs to -- quant tags,
# precision, role words. Stripped before comparing a projector to the weights.
_GENERIC_TOKENS = {
    "mmproj", "mproj", "model", "gguf", "instruct", "it", "chat", "base",
    "abliterated", "uncensored", "f16", "f32", "bf16", "fp16", "fp32",
    "q2", "q3", "q4", "q5", "q6", "q8", "k", "l", "m", "s", "xl", "xs", "xxs",
    "0", "1", "ud", "iq2", "iq3", "iq4", "gsq", "rco", "mtp", "of",
}

_SIZE_RE = re.compile(r'^\d+(?:\.\d+)?b$')


def _ident_tokens(filename: str) -> set[str]:
    """The tokens in a GGUF filename that actually identify the model family."""
    stem = re.sub(r'\.gguf$', '', os.path.basename(filename), flags=re.I).lower()
    parts = re.split(r'[-_.\s]+', stem)
    return {p for p in parts if p and p not in _GENERIC_TOKENS}


def _projector_matches(proj: str, weights: str) -> bool:
    """Does this projector belong to these weights?

    Projectors are NOT interchangeable -- an mmproj built for Qwen3-VL-8B produces
    embeddings a 27B model cannot read, and llama-server will either refuse to load
    or emit nonsense. Two naming conventions exist in the wild:

      * generic  -- 'mmproj-model-bf16.gguf' (huihui-ai's convention): carries no
        family token, so it is taken to belong to whatever single model is served.
      * specific -- 'mmproj-Qwen3-VL-8B-Instruct-F16.gguf': must agree with the
        weights on family AND parameter size before we trust it.
    """
    ptoks = _ident_tokens(proj)
    wtoks = _ident_tokens(weights)
    if not ptoks:
        return True                                  # generic -> assume it pairs
    psize = {t for t in ptoks if _SIZE_RE.match(t)}
    wsize = {t for t in wtoks if _SIZE_RE.match(t)}
    if psize and wsize and not (psize & wsize):
        return False                                 # 8B projector, 27B weights
    family_p = ptoks - psize
    family_w = wtoks - wsize
    if family_p and not (family_p & family_w):
        return False                                 # different model family
    return True


def mmproj_path() -> str | None:
    """The multimodal projector that pairs with the chosen weights, if present.
    Returns None when nothing in models/ credibly matches -- better text-only than
    a projector that silently garbles every frame. KAMIL_MMPROJ overrides."""
    env = os.getenv("KAMIL_MMPROJ")
    if env:
        p = Path(env).expanduser()
        return str(p) if p.is_file() else None
    cands = [p for p in glob.glob(os.path.join(str(MODELS_DIR), "*.gguf"))
             if _is_mmproj(p)]
    if not cands:
        return None
    mp = model_path()
    if not mp:
        return None
    viable = [c for c in cands if _projector_matches(c, mp)]
    if not viable:
        return None
    # a family-specific match beats a generic one
    viable.sort(key=lambda c: (bool(_ident_tokens(c)), os.path.getsize(c)), reverse=True)
    return viable[0]


def is_multimodal() -> bool:
    """Can the primary model see? KAMIL_PRIMARY_MULTIMODAL forces the answer;
    otherwise it is yes exactly when we have both weights and a projector."""
    env = os.getenv("KAMIL_PRIMARY_MULTIMODAL")
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(model_path() and mmproj_path())


def label() -> str:
    """A short human name for the loaded model, for the UI and logs."""
    mp = model_path()
    if not mp:
        return "no model"
    name = os.path.basename(mp)
    name = re.sub(r'\.gguf$', '', name, flags=re.I)
    name = re.sub(r'-\d+-of-\d+$', '', name)
    return name + (" (vision)" if is_multimodal() else "")


def missing_reason() -> str:
    """Why the model will not serve, in words a human can act on. '' when fine."""
    if not model_path():
        return (f"no usable GGUF in {MODELS_DIR}. Download "
                f"huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF (e.g. the Q4_K file) "
                f"into that directory, or point KAMIL_MODEL at one.")
    if not mmproj_path():
        return ("model found but no mmproj-*.gguf projector, so Kamil is text-only: "
                "the webcam monitor cannot describe what it sees. Download "
                "mmproj-model-bf16.gguf from the same repo into models/.")
    return ""


def serve_args(port: int | None = None, ctx: int | None = None) -> list[str]:
    """The full llama-server argv for the one model, with offload flags fitted to
    this machine's VRAM by core.hwfit. The launchers call this through __main__ so
    there is exactly one source of truth for the flags."""
    from core import hwfit

    mp = model_path()
    if not mp:
        raise RuntimeError(missing_reason())
    port = port or PRIMARY_PORT
    ctx = ctx or MODEL_CTX

    meta = hwfit.model_meta(mp)
    profile = hwfit.fit_profile(meta, ctx=ctx)
    args = hwfit.serve_args(mp, profile, port)

    proj = mmproj_path()
    if proj:
        args += ["--mmproj", proj]
    return args


def describe() -> dict:
    """Everything the UI and /health need to know about the single model."""
    mp = model_path()
    info = {
        "model": mp,
        "label": label(),
        "mmproj": mmproj_path(),
        "multimodal": is_multimodal(),
        "ctx": MODEL_CTX,
        "port": PRIMARY_PORT,
        "reason": missing_reason(),
    }
    if mp:
        try:
            from core import hwfit
            meta = hwfit.model_meta(mp)
            info["fit"] = hwfit.fit_profile(meta, ctx=MODEL_CTX)
            info["weights_gb"] = meta["weights_gb"]
        except Exception:
            pass
    return info


if __name__ == "__main__":
    # `python -m core.model --serve-args` prints the argv one per line, which is how
    # start.sh / start.bat / the Electron shell learn the flags without duplicating
    # any of this logic.
    import json
    import sys

    if "--serve-args" in sys.argv:
        try:
            print("\n".join(serve_args()))
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
    elif "--bin" in sys.argv:
        print(llama_server_bin())
    else:
        print(json.dumps(describe(), indent=2, default=str))
