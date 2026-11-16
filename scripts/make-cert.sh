#!/usr/bin/env bash
# scripts/make-cert.sh -- (re)generate Kamil's self-signed TLS pair.
#
# Why this exists as a script: the previous kamil.key was committed to what became
# a PUBLIC repository, so it had to be treated as compromised and replaced. Having
# rotation be one command means it is not a research project the next time.
#
# The pair is issued for LOOPBACK ONLY -- localhost, 127.0.0.1, ::1. The old cert
# was CN=192.168.1.7, from when Kamil served HTTPS to other devices on the LAN.
# That is no longer the architecture: run.py binds 127.0.0.1 and the backend
# requires KAMIL_API_TOKEN, because it exposes shell and file tools. Re-issuing a
# LAN certificate would be rebuilding a door that was deliberately bricked up.
#
#   ./scripts/make-cert.sh                      # loopback pair, 2 years
#   HOSTS="localhost,kamil.local" ./scripts/...  # extra names if you need them
#   DAYS=365 ./scripts/make-cert.sh
#
# Nothing in Kamil currently loads these files (uvicorn.run takes no ssl_* args),
# so this is precautionary. To actually serve HTTPS, pass them to uvicorn:
#   uvicorn.run(app, host=..., port=..., ssl_keyfile='kamil.key', ssl_certfile='kamil.crt')
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KEY="${KEY:-kamil.key}"
CRT="${CRT:-kamil.crt}"
DAYS="${DAYS:-730}"
HOSTS="${HOSTS:-localhost}"

say() { printf '\033[36m[cert]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[cert]\033[0m %s\n' "$*" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || die "openssl not found (dnf install openssl)"

# Build the SAN list: every name in HOSTS as a DNS entry, plus both loopback IPs.
SAN=""
IFS=',' read -ra NAMES <<< "$HOSTS"
for n in "${NAMES[@]}"; do
  n="$(echo "$n" | tr -d '[:space:]')"
  [[ -n "$n" ]] || continue
  if [[ "$n" =~ ^[0-9.]+$ || "$n" == *:* ]]; then
    SAN+="IP:$n,"
  else
    SAN+="DNS:$n,"
  fi
done
# ::1 written out in full: the compact form trips some openssl argument parsers.
SAN+="IP:127.0.0.1,IP:0:0:0:0:0:0:0:1"

# Keep the old pair aside rather than clobbering it, so a running service that
# still holds the old one can be pointed back if something breaks.
#
# The backup name keeps the ORIGINAL EXTENSION -- "kamil.replaced-X.key", not
# "kamil.key.replaced-X". That is not cosmetic: .gitignore excludes *.key, and a
# name ending in ".replaced-X" slips past it, which would leave a retired private
# key sitting in the repo one `git add -A` away from a public push. That is
# precisely how the key this script exists to replace got published.
STAMP="$(date +%Y%m%d_%H%M%S)"
for f in "$KEY" "$CRT"; do
  if [[ -f "$f" ]]; then
    ext="${f##*.}"
    base="${f%.*}"
    backup="$base.replaced-$STAMP.$ext"
    mv "$f" "$backup"
    say "kept previous $f as $backup"
  fi
done

say "issuing a new pair for: $SAN"

# Everything goes through a config file rather than -subj/-addext. Those take a
# leading-slash DN, which Git Bash's MSYS layer rewrites into a Windows path
# ("/CN=localhost" -> "C:/Program Files/Git/CN=localhost"), so the same command
# that works on Fedora fails on Windows. A config file has no such ambiguity.
CONF="$(mktemp)"
trap 'rm -f "$CONF"' EXIT
{
  echo "[req]"
  echo "default_md = sha256"
  echo "prompt = no"
  echo "distinguished_name = dn"
  echo "x509_extensions = v3"
  echo
  echo "[dn]"
  echo "CN = localhost"
  echo "O = Kamil (local)"
  echo
  echo "[v3]"
  echo "subjectAltName = $SAN"
  echo "keyUsage = critical,digitalSignature,keyEncipherment"
  echo "extendedKeyUsage = serverAuth"
  echo "basicConstraints = critical,CA:FALSE"
} > "$CONF"

openssl req -x509 -config "$CONF" \
  -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout "$KEY" -out "$CRT" \
  -days "$DAYS" -noenc

# The key must not be world-readable. On Windows this is a no-op; on Fedora it
# matters, because anything running as another user could otherwise read it.
chmod 600 "$KEY" 2>/dev/null || true
chmod 644 "$CRT" 2>/dev/null || true

say "done"
openssl x509 -in "$CRT" -noout -subject -dates -ext subjectAltName
openssl x509 -in "$CRT" -noout -fingerprint -sha256

cat <<'MSG'

Both files are gitignored (*.key / *.crt). Never commit them.

If you kept .replaced-* copies and nothing broke, shred them -- a retired
private key is still a private key:
  shred -u kamil.replaced-*.key 2>/dev/null || rm -f kamil.replaced-*.key
  rm -f kamil.replaced-*.crt
MSG
