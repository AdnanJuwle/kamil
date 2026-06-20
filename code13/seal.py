"""
code13/seal.py — Kamil's sealed-directive cryptography.

Stdlib-only authenticated encryption. No third-party dependency, nothing leaves
the machine. Inspired by the Kootlipi password encoder in this same directory
(deterministic PBKDF2-HMAC-SHA256, 200,000 iterations): the key exists only
while the passphrase is held in memory. Nothing written to disk — not this file,
not the sealed blob, not the system prompt — reveals the passphrase or the
plaintext it guards.

Construction (KAMIL-SEAL-v1):
    key material = PBKDF2-HMAC-SHA256(passphrase, salt, 200_000, dklen=64)
                   -> enc_key (32 bytes) || mac_key (32 bytes)
    keystream    = HMAC-SHA256(enc_key, nonce || counter) concatenated  (CTR-style PRF)
    ciphertext   = plaintext XOR keystream
    tag          = HMAC-SHA256(mac_key, nonce || ciphertext)             (encrypt-then-MAC)

unseal() returns None on any MAC mismatch. A wrong passphrase is therefore
mathematically indistinguishable from a tampered file, and leaks nothing about
the real passphrase or the contents.

NOTE ON STRENGTH: a passphrase that is a single dictionary word is, by nature,
vulnerable to an offline wordlist attack by anyone who holds this sealed file
and this source. PBKDF2 slows that down; it does not stop it. The real second
factor is the heir's verified voice (see core/succession.py). Treat the word as
"won't be found by reading the code," not "uncrackable."

CLI:
    python seal.py seal   [--in FILE] [--out FILE]   # plaintext in (or stdin) -> sealed
    python seal.py unseal [--in FILE]                # sealed in -> plaintext (or FAILED)
    python seal.py verify [--in FILE]                # exit 0 if passphrase opens it
Passphrase is read from $KAMIL_SEAL_PW if set, else prompted (never echoed).
"""

import base64
import hashlib
import hmac
import json
import os
import sys
from typing import Optional

PBKDF2_ITERS = 200_000
_MAGIC = "KAMIL-SEAL-v1"


def _derive(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERS, dklen=64
    )
    return dk[:32], dk[32:]


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(
            hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(out[:n])


def seal(plaintext: str, passphrase: str) -> str:
    """Encrypt plaintext under passphrase. Returns a JSON blob (safe to store/commit)."""
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = _derive(passphrase, salt)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(enc_key, nonce, len(pt))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return json.dumps(
        {
            "magic": _MAGIC,
            "iters": PBKDF2_ITERS,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode(),
            "tag": base64.b64encode(tag).decode(),
        },
        indent=2,
    )


def unseal(blob_text: str, passphrase: str) -> Optional[str]:
    """Return plaintext if passphrase opens the blob, else None. Never raises."""
    try:
        blob = json.loads(blob_text)
        if blob.get("magic") != _MAGIC:
            return None
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        ct = base64.b64decode(blob["ct"])
        tag = base64.b64decode(blob["tag"])
        enc_key, mac_key = _derive(passphrase, salt)
        expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            return None
        pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
        return pt.decode("utf-8")
    except Exception:
        return None


def opens(blob_text: str, passphrase: str) -> bool:
    """True iff passphrase decrypts the blob. Used as the succession trigger test."""
    return unseal(blob_text, passphrase) is not None


# ── CLI ───────────────────────────────────────────────────────────

def _get_pass() -> str:
    pw = os.environ.get("KAMIL_SEAL_PW")
    if pw:
        return pw
    import getpass
    return getpass.getpass("Passphrase: ")


def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("seal", "unseal", "verify"):
        print(__doc__)
        return 2
    cmd = argv[0]
    in_file = out_file = None
    i = 1
    while i < len(argv):
        if argv[i] == "--in":
            in_file = argv[i + 1]; i += 2
        elif argv[i] == "--out":
            out_file = argv[i + 1]; i += 2
        else:
            i += 1

    data = open(in_file, "r", encoding="utf-8").read() if in_file else sys.stdin.read()
    pw = _get_pass()

    if cmd == "seal":
        blob = seal(data, pw)
        if out_file:
            open(out_file, "w", encoding="utf-8").write(blob)
            print(f"Sealed -> {out_file}")
        else:
            sys.stdout.write(blob)
        return 0

    if cmd == "unseal":
        pt = unseal(data, pw)
        if pt is None:
            print("FAILED: wrong passphrase or tampered blob", file=sys.stderr)
            return 1
        sys.stdout.write(pt)
        return 0

    # verify
    ok = opens(data, pw)
    print("OK" if ok else "NO")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
