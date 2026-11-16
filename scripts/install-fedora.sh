#!/usr/bin/env bash
# scripts/install-fedora.sh -- set Kamil up on a fresh Fedora box.
#
# Installs the system packages Kamil actually reaches for, creates the venv, and
# checks the webcam. It does NOT build llama.cpp (scripts/build-llama-cpp.sh) and it
# does NOT download models -- both are large and deliberate steps.
#
#   ./scripts/install-fedora.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
say() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[setup]\033[0m %s\n' "$*"; }

# -- system packages
# python3.13: uvicorn + threading is broken on 3.14, so pin the interpreter
# ffmpeg-free: webm->wav for transcription and voice enrollment
# v4l-utils: webcam enumeration/diagnostics for the room watcher
# psmisc: fuser, used by core/paths.free_port_cmd to clear a stale model port
PKGS=(
  python3.13 python3.13-devel
  gcc gcc-c++ make cmake git
  ffmpeg-free
  v4l-utils
  psmisc lsof
  portaudio-devel       # sounddevice/pyaudio for mic capture
  mesa-libGL            # opencv runtime dep
  sqlite
)
say "installing system packages"
if ! sudo dnf install -y "${PKGS[@]}"; then
  warn "ffmpeg-free failed? If you use RPM Fusion, try: sudo dnf install -y ffmpeg"
  sudo dnf install -y "${PKGS[@]/ffmpeg-free/}" || true
fi

# -- interpreter
PY_BIN="$(command -v python3.13 || true)"
if [[ -z "${PY_BIN:-}" ]]; then
  warn "python3.13 not found; falling back to $(python3 --version)"
  warn "NOTE: do not use 3.14 -- uvicorn + threading breaks on it"
  PY_BIN="$(command -v python3)"
fi
say "interpreter: $PY_BIN ($("$PY_BIN" --version))"

# -- venv (kept at venv313 so the name matches every doc and the Electron shell)
if [[ ! -d "$ROOT/venv313" ]]; then
  say "creating venv313"
  "$PY_BIN" -m venv venv313
fi
VPY="$ROOT/venv313/bin/python3"
[[ -x "$VPY" ]] || VPY="$ROOT/venv313/bin/python"

say "installing Python dependencies (this takes a few minutes)"
"$VPY" -m pip install --upgrade pip wheel
if [[ -f requirements.lock.txt ]]; then
  # The lock file was frozen on Windows; a few wheels there are platform-specific,
  # so fall back to the curated list if it will not resolve.
  "$VPY" -m pip install -r requirements.lock.txt || {
    warn "lock file did not resolve on Linux -- using requirements.txt"
    "$VPY" -m pip install -r requirements.txt
  }
else
  "$VPY" -m pip install -r requirements.txt
fi
[[ -f requirements-dev.txt ]] && "$VPY" -m pip install -r requirements-dev.txt || true

# -- webcam check (the room watcher needs this)
say "checking webcam"
if command -v v4l2-ctl >/dev/null 2>&1; then
  if v4l2-ctl --list-devices 2>/dev/null | grep -q .; then
    v4l2-ctl --list-devices 2>/dev/null | sed 's/^/        /'
  else
    warn "no /dev/video* device found -- the room watcher will retry with backoff"
  fi
fi
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx video; then
  warn "$USER is not in the 'video' group. The watcher may not open the camera."
  warn "  sudo usermod -aG video $USER     # then log out and back in"
fi

# -- directories the watcher and logs use
mkdir -p data/room data/logs

say "verifying Kamil's own view of this machine"
"$VPY" - <<'EOF'
import sys
sys.path.insert(0, ".")
from core import paths, model
print(f"        OS            {paths.SYSTEM}")
print(f"        root          {paths.KAMIL_ROOT}")
print(f"        projects      {paths.PROJECTS_ROOT}")
print(f"        llama-server  {paths.llama_server_bin()}")
print(f"        ffmpeg        {paths.ffmpeg_bin()}")
print(f"        model         {model.label()}")
reason = model.missing_reason()
if reason:
    print(f"        TODO          {reason}")
EOF

cat <<'MSG'

Next:
  1. scripts/build-llama-cpp.sh        # CURRENT llama.cpp (qwen35 arch + mtmd vision)
  2. download the model into models/:
       huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF
         Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf     (~16.8 GB, fits a 24GB card)
         mmproj-model-bf16.gguf                       (the vision projector -- required
                                                        for the webcam to be understood)
  3. ./start.sh
  4. optional, run at login:   scripts/install-systemd.sh

Read docs/FEDORA.md for the system-control and webcam specifics.
MSG
