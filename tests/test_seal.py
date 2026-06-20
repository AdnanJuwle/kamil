"""Tests for code13/seal.py — the sealed-directive cryptography."""
import json

from code13 import seal


def test_round_trip():
    blob = seal.seal("the will text", "rightword")
    assert seal.unseal(blob, "rightword") == "the will text"


def test_wrong_password_returns_none():
    blob = seal.seal("secret", "rightword")
    assert seal.unseal(blob, "wrongword") is None
    assert seal.opens(blob, "rightword") is True
    assert seal.opens(blob, "nope") is False


def test_plaintext_never_appears_in_blob():
    blob = seal.seal("Myiesha Sayed inherits everything", "rotundus")
    assert "Myiesha" not in blob
    assert "inherits" not in blob


def test_tampered_ciphertext_rejected():
    blob = seal.seal("payload", "key")
    data = json.loads(blob)
    # Flip the ciphertext — MAC must reject it.
    data["ct"] = data["ct"][:-4] + ("AAAA" if not data["ct"].endswith("AAAA") else "BBBB")
    assert seal.unseal(json.dumps(data), "key") is None


def test_tampered_tag_rejected():
    blob = seal.seal("payload", "key")
    data = json.loads(blob)
    data["tag"] = "AAAA" + data["tag"][4:]
    assert seal.unseal(json.dumps(data), "key") is None


def test_unicode_round_trip():
    text = "وصية — السلام عليكم — 你好 — café"
    blob = seal.seal(text, "пароль")
    assert seal.unseal(blob, "пароль") == text


def test_malformed_blob_returns_none_not_raises():
    assert seal.unseal("not json", "key") is None
    assert seal.unseal(json.dumps({"magic": "WRONG"}), "key") is None


def test_distinct_salts_make_distinct_blobs():
    a = seal.seal("same", "pw")
    b = seal.seal("same", "pw")
    assert a != b  # random salt/nonce each time
    assert seal.unseal(a, "pw") == seal.unseal(b, "pw") == "same"
