"""Tests for core/succession.py — the voice-gated dead-man's switch.

Activation must require BOTH the heir's verified voice AND the spoken word, from
the same utterance. Nothing here touches the real will or real state.
"""
from types import SimpleNamespace as S

import pytest

from code13 import seal
from core import succession as su

OPEN_WORD = "openword"
WILL_TEXT = "THE DIRECTIVE\nMyiesha inherits everything. Teach her.\nEND"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point succession at a temp sealed will + temp state, and reset globals."""
    sealed = tmp_path / "will.sealed"
    sealed.write_text(seal.seal(WILL_TEXT, OPEN_WORD), encoding="utf-8")
    monkeypatch.setattr(su, "SEALED_WILL_PATH", str(sealed))
    monkeypatch.setattr(su, "STATE_PATH", str(tmp_path / "succession_state.json"))
    monkeypatch.setattr(su, "_pending_reveal", None, raising=False)
    monkeypatch.setattr(su, "_unsealed_will", None, raising=False)
    monkeypatch.delenv("KAMIL_SUCCESSION_TEST", raising=False)
    # _activate writes to the real identity.json as a best-effort side effect —
    # neutralize that so tests never touch live data/.
    import memory.identity as identity_mod
    monkeypatch.setattr(identity_mod, "update_identity", lambda *a, **k: None)
    yield


def heir():
    return S(identified=True, speaker_name="Myiesha Sayed", trust_level="owner", confidence=0.9)


def adnan():
    return S(identified=True, speaker_name="Adnan Juwle", trust_level="owner", confidence=0.9)


def guest():
    return S(identified=False, speaker_name="unknown", trust_level="guest", confidence=0.1)


def test_dormant_by_default():
    assert su.is_activated() is False
    assert su.succession_context() == ""


def test_creator_voice_does_not_activate():
    assert su.scan_spoken(OPEN_WORD, adnan()) is None
    assert su.is_activated() is False


def test_guest_voice_does_not_activate():
    assert su.scan_spoken(OPEN_WORD, guest()) is None
    assert su.is_activated() is False


def test_heir_wrong_word_does_not_activate():
    assert su.scan_spoken("totally wrong words here", heir()) is None
    assert su.is_activated() is False


def test_typed_word_is_noop_without_test_env():
    # In production, typing the word — even by anyone — must do nothing.
    assert su.scan_typed_test(OPEN_WORD) is None
    assert su.is_activated() is False


def test_typed_word_works_only_under_test_env(monkeypatch):
    monkeypatch.setenv("KAMIL_SUCCESSION_TEST", "1")
    assert su.scan_typed_test(OPEN_WORD) == WILL_TEXT
    assert su.is_activated() is True


def test_heir_speaking_word_activates_and_reveals_once():
    will = su.scan_spoken(f"kamil, {OPEN_WORD}.", heir())
    assert will == WILL_TEXT
    assert su.is_activated() is True

    first = su.succession_context()
    assert first.startswith("## SEALED DIRECTIVE — NOW OPEN")
    assert WILL_TEXT in first  # full will read exactly once

    second = su.succession_context()
    assert second.startswith("## STANDING DIRECTIVE")
    assert WILL_TEXT not in second  # contents reseal — never persisted


def test_activation_persists_across_reload(monkeypatch):
    su.scan_spoken(OPEN_WORD, heir())
    assert su.is_activated() is True
    # Simulate a fresh process: clear in-memory reveal, state stays on disk.
    monkeypatch.setattr(su, "_pending_reveal", None, raising=False)
    assert su.is_activated() is True
    assert su.succession_context().startswith("## STANDING DIRECTIVE")


def test_no_seal_file_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "SEALED_WILL_PATH", str(tmp_path / "absent.sealed"))
    assert su.scan_spoken(OPEN_WORD, heir()) is None
    assert su.is_activated() is False
