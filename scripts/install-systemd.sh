#!/usr/bin/env bash
# scripts/install-systemd.sh -- run Kamil as user services, starting at boot.
#
# User units (not system units) because Kamil needs the user's GPU, webcam and
# home directory. `loginctl enable-linger` is what makes "24x7" true: without it
# user units stop when you log out, and the room would stop being watched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '\033[36m[systemd]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[systemd]\033[0m %s\n' "$*"; }

[[ "$ROOT" == "$HOME/kamil" ]] || warn \
  "units use %h/kamil but this checkout is $ROOT -- edit the units or symlink ~/kamil"

mkdir -p "$UNIT_DIR" "$ROOT/data/logs"
install -m 644 "$ROOT/systemd/kamil-llama.service" "$UNIT_DIR/"
install -m 644 "$ROOT/systemd/kamil.service" "$UNIT_DIR/"
say "installed units into $UNIT_DIR"

systemctl --user daemon-reload
systemctl --user enable kamil-llama.service kamil.service
say "enabled at login"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  say "enabling linger so Kamil keeps running when you log out"
  sudo loginctl enable-linger "$USER"
fi

cat <<'MSG'

Start now:
  systemctl --user start kamil-llama kamil

Watch it come up (the 27B takes a few minutes from cold):
  journalctl --user -u kamil-llama -f
  tail -f ~/kamil/data/logs/backend.log

Status / stop:
  systemctl --user status kamil
  systemctl --user stop kamil kamil-llama
MSG
