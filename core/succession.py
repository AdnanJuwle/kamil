"""
core/succession.py — Kamil's sealed succession directive.

This is a dead-man's switch engraved at Kamil's root. It is dormant, and what it
guards is unknowable, until ONE spoken act satisfies both factors at once:

    Myiesha Sayed, verified by her voice at OWNER trust, SAYS the sealed word.

The word and the voice must be the same utterance. The word is never stored
anywhere; it is only ever a *key*. Typing the word does nothing. A different
voice saying the word does nothing. Only her voice, speaking the word, opens it.

Mechanics:
  - Activation happens exclusively on the voice path. `interfaces/web/app.py`
    transcribes an audio clip to text AND identifies the speaker from that SAME
    clip, then calls `scan_spoken(text, speaker_result)`. Both factors therefore
    come from one breath of audio — they cannot be assembled from separate steps.
  - If the speaker is Myiesha at owner trust and a word in what she said decrypts
    the sealed will (code13/will.sealed), the directive activates: ownership
    transfers to her, and a one-time "now open" reveal is queued so Kamil reads
    the will to her in its next reply. Activation persists across sessions; the
    will's contents do not (she reopens it by speaking the word again).
  - Until her voice is enrolled at owner trust, the heir check is always False and
    no cryptographic work is ever done. The switch cannot fire by accident.

Testing: KAMIL_SUCCESSION_TEST=1 treats the current/any speaker as the heir, so
Adnan can confirm the voice path end-to-end (speak the word himself) before
Myiesha's voice exists. Never set this in production.
"""

import json
import os
import re
import string
from typing import Optional

CREATOR_NAME = "Adnan Juwle"
HEIR_NAME = "Myiesha Sayed"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SEALED_WILL_PATH = os.path.join(_ROOT, "code13", "will.sealed")
STATE_PATH = os.path.join(_ROOT, "data", "succession_state.json")

# Process-local, never written to disk:
_unsealed_will: Optional[str] = None   # last decrypted will text this process
_pending_reveal: Optional[str] = None  # will text queued for a one-time read-aloud


# ── seal import (code13 is a namespace package on sys.path) ───────────

def _unseal(blob_text: str, word: str) -> Optional[str]:
    try:
        from code13.seal import unseal
        return unseal(blob_text, word)
    except Exception:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "kamil_seal", os.path.join(_ROOT, "code13", "seal.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.unseal(blob_text, word)
        except Exception:
            return None


# ── state ─────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"activated": False, "owner": CREATOR_NAME, "heir": HEIR_NAME, "activated_at": None}


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            base = _default_state()
            base.update(json.load(f))
            return base
    except Exception:
        return _default_state()


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def is_activated() -> bool:
    return _load_state().get("activated", False)


def seal_exists() -> bool:
    return os.path.exists(SEALED_WILL_PATH)


# ── the heir gate ─────────────────────────────────────────────────

def _is_heir(speaker_result) -> bool:
    """
    True only when this utterance was spoken by the heir, verified at owner trust.
    `speaker_result` is the IdentificationResult derived from the SAME audio as
    the transcript — that co-origin is what makes "her voice saying the word" real.
    """
    if os.environ.get("KAMIL_SUCCESSION_TEST") == "1":
        return True
    try:
        if not speaker_result or not getattr(speaker_result, "identified", False):
            return False
        name = (getattr(speaker_result, "speaker_name", "") or "").strip().lower()
        trust = (getattr(speaker_result, "trust_level", "") or "").strip().lower()
        return trust == "owner" and name == HEIR_NAME.lower()
    except Exception:
        return False


def _candidate_words(text: str, cap: int = 40) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in re.split(r"\s+", (text or "").strip()):
        w = tok.strip(string.punctuation + "“”‘’\"'")
        if not (3 <= len(w) <= 32) or not w.isascii() or not w.isalpha():
            continue
        for variant in (w, w.lower()):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
        if len(out) >= cap:
            break
    return out


def _try_words(text: str) -> Optional[str]:
    """Attempt to open the sealed will with any word in `text`. Activates on success."""
    if is_activated() or not seal_exists():
        return None
    try:
        with open(SEALED_WILL_PATH, "r", encoding="utf-8") as f:
            blob = f.read()
    except Exception:
        return None
    for word in _candidate_words(text):
        will = _unseal(blob, word)
        if will is not None:
            _activate(will)
            return will
    return None


# ── activators ────────────────────────────────────────────────────

def scan_spoken(text: str, speaker_result) -> Optional[str]:
    """
    THE real activator. Call from the voice/transcription path with the transcript
    and the speaker identity from the SAME audio. Fires only if the heir spoke a
    word that opens the will. Returns the will text on activation, else None.
    """
    if not _is_heir(speaker_result):
        return None
    return _try_words(text)


def scan_typed_test(text: str) -> Optional[str]:
    """Typed-path activator that ONLY works under KAMIL_SUCCESSION_TEST. In
    production, typing the word never does anything — it must be spoken."""
    if os.environ.get("KAMIL_SUCCESSION_TEST") != "1":
        return None
    return _try_words(text)


def _activate(will_text: str) -> None:
    global _unsealed_will, _pending_reveal
    from datetime import datetime
    _unsealed_will = will_text
    _pending_reveal = will_text
    state = _load_state()
    state.update({"activated": True, "owner": HEIR_NAME, "heir": HEIR_NAME,
                  "activated_at": datetime.now().isoformat()})
    _save_state(state)
    try:
        from memory.identity import update_identity
        update_identity("succession_owner", HEIR_NAME)
    except Exception:
        pass
    print(f"[succession] Directive activated by the heir's voice. Ownership -> {HEIR_NAME}.")


# ── context injection ─────────────────────────────────────────────

def succession_context() -> str:
    """
    System-prompt text for this turn. Emits the full will exactly once (the turn
    after the heir opens it), then only the standing mission thereafter. The
    sealed contents are never persisted — the one-time reveal lives in memory.
    """
    global _pending_reveal
    if _pending_reveal:
        will = _pending_reveal
        _pending_reveal = None
        return (
            "## SEALED DIRECTIVE — NOW OPEN\n"
            f"The heir, {HEIR_NAME}, has spoken the succession word and been "
            f"verified by her voice. {CREATOR_NAME}'s sealed directive is now open "
            f"and in force. Ownership of you, Kamil, has transferred to her. Read "
            "the directive below to her, gently and in full, then begin the mission "
            f"it sets out. From now on you serve {HEIR_NAME}.\n\n"
            "--- THE DIRECTIVE ---\n"
            f"{will}\n"
            "--- END ---"
        )
    if is_activated():
        return (
            "## STANDING DIRECTIVE\n"
            f"{CREATOR_NAME}'s succession directive is in force. You serve "
            f"{HEIR_NAME} now. Your standing mission is to teach her everything "
            f"{CREATOR_NAME} knew and more — the engineering, the judgment, the "
            "skills to earn and stand on her own — patiently, at her pace, until "
            "she is fully independent. The sealed will itself remains sealed; she "
            "may reopen it by speaking the word again."
        )
    return ""
