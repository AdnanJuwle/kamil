#!/usr/bin/env bash
# start.sh -- bring Kamil up on Linux/macOS.
#
# The POSIX counterpart to start.bat, and the single-model equivalent of it: one
# llama-server holding Huihui-Qwen3.8-27B-abliterated (chat AND vision), plus the
# Python backend. There is no second voice model and no separate vision model any
# more -- see core/model.py.
#
# The llama-server flags are NOT written here. `python -m core.model --serve-args`
# computes them from the GGUF actually present and this machine's VRAM (core/hwfit),
# so this script never goes stale when you change quant or GPU.
#
#   ./start.sh            # start model + backend, wait for health
#   ./start.sh --no-model # backend only (a model server is already running)
#   ./start.sh --stop     # stop whatever this script started
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${KAMIL_PORT:-3000}"
PRIMARY_PORT="${KAMIL_PRIMARY_PORT:-8080}"
RUN_DIR="${XDG_RUNTIME_DIR:-/tmp}/kamil"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# -- interpreter: prefer the project venv, fall back to python3
if [[ -x "$ROOT/venv313/bin/python3" ]]; then
  PY="$ROOT/venv313/bin/python3"
elif [[ -x "$ROOT/venv313/bin/python" ]]; then
  PY="$ROOT/venv313/bin/python"
elif [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="$(command -v python3 || command -v python)"
fi
[[ -n "${PY:-}" ]] || { echo "no python found; run scripts/install-fedora.sh first" >&2; exit 1; }

say() { printf '\033[36m[kamil]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[kamil]\033[0m %s\n' "$*" >&2; exit 1; }

stop_all() {
  for name in llama backend; do
    pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        say "stopping $name (pid $pid)"
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
      fi
      rm -f "$pidfile"
    fi
  done
}

if [[ "${1:-}" == "--stop" ]]; then stop_all; say "stopped"; exit 0; fi
trap 'echo; say "shutting down"; stop_all; exit 0' INT TERM

# -- the model
if [[ "${1:-}" != "--no-model" ]]; then
  LLAMA_BIN="$("$PY" -m core.model --bin)"
  command -v "$LLAMA_BIN" >/dev/null 2>&1 || [[ -x "$LLAMA_BIN" ]] || \
    die "llama-server not found at '$LLAMA_BIN'. Build it: scripts/build-llama-cpp.sh
     (Huihui-Qwen3.8 needs a CURRENT llama.cpp -- its architecture is 'qwen35')"

  mapfile -t SERVE_ARGS < <("$PY" -m core.model --serve-args) || \
    die "$("$PY" -m core.model --serve-args 2>&1 >/dev/null)"
  [[ ${#SERVE_ARGS[@]} -gt 0 ]] || die "could not compute llama-server flags"

  say "model: $("$PY" -c 'from core import model; print(model.label())')"
  say "flags: ${SERVE_ARGS[*]}"
  "$LLAMA_BIN" "${SERVE_ARGS[@]}" >"$LOG_DIR/llama.log" 2>&1 &
  echo $! > "$RUN_DIR/llama.pid"
  say "llama-server starting on :$PRIMARY_PORT (log: data/logs/llama.log)"

  say "waiting for weights to load (a 27B takes a while)..."
  for i in $(seq 600); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PRIMARY_PORT/health" 2>/dev/null || true)"
    [[ "$code" == "200" ]] && { say "model ready"; break; }
    kill -0 "$(cat "$RUN_DIR/llama.pid")" 2>/dev/null || die "llama-server died -- see data/logs/llama.log"
    sleep 1
    [[ $i -eq 600 ]] && die "model did not become healthy in 10 minutes"
  done
fi

# -- the backend (heavy init: whisper, embeddings, chroma -- 30-60s on first boot)
say "starting backend on :$PORT"
"$PY" run.py >"$LOG_DIR/backend.log" 2>&1 &
echo $! > "$RUN_DIR/backend.pid"

for i in $(seq 300); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null || true)"
  [[ "$code" =~ ^(200|401)$ ]] && { say "backend up -> http://localhost:$PORT"; break; }
  kill -0 "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null || die "backend died -- see data/logs/backend.log"
  sleep 1
done

say "room watch: $("$PY" -c 'from core import room_watch as r; print("on, camera %s" % r.CAMERA_INDEX if r.ENABLED else "off (KAMIL_ROOM_WATCH=0)")')"
say "running. Ctrl-C to stop both."
wait
