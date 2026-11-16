#!/usr/bin/env bash
# scripts/build-llama-cpp.sh -- build a CURRENT llama.cpp with CUDA on Fedora.
#
# Why this is mandatory rather than optional: Huihui-Qwen3.8-27B-abliterated reports
# its architecture as "qwen35", and its vision path goes through llama.cpp's mtmd
# (multimodal) support. A llama.cpp built before that architecture landed will
# refuse to load the GGUF outright -- the model card says plainly "Use the latest
# llama.cpp". The old Windows build at C:\llama.cpp will not do.
#
#   ./scripts/build-llama-cpp.sh                 # CUDA build into ~/llama.cpp
#   LLAMA_PREFIX=/opt/llama.cpp ./scripts/...    # somewhere else
#   LLAMA_CUDA=0 ./scripts/build-llama-cpp.sh    # CPU-only
set -euo pipefail

PREFIX="${LLAMA_PREFIX:-$HOME/llama.cpp}"
USE_CUDA="${LLAMA_CUDA:-1}"
JOBS="${JOBS:-$(nproc)}"

say() { printf '\033[36m[llama]\033[0m %s\n' "$*"; }

say "checking build dependencies"
MISSING=()
for pkg in gcc-c++ cmake git make; do
  rpm -q "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
# curl-devel gives llama-server its HTTP model fetching; libcurl is needed to link
rpm -q libcurl-devel >/dev/null 2>&1 || MISSING+=(libcurl-devel)
if [[ ${#MISSING[@]} -gt 0 ]]; then
  say "installing: ${MISSING[*]}"
  sudo dnf install -y "${MISSING[@]}"
fi

if [[ "$USE_CUDA" == "1" ]]; then
  if ! command -v nvcc >/dev/null 2>&1; then
    cat >&2 <<'MSG'
nvcc not found. CUDA on Fedora needs the NVIDIA repo + cuda-toolkit:

  sudo dnf install -y https://developer.download.nvidia.com/compute/cuda/repos/fedora41/x86_64/cuda-fedora41.repo
  sudo dnf install -y cuda-toolkit akmod-nvidia
  # reboot, then re-run this script

Or build CPU-only (a 27B will be painfully slow):  LLAMA_CUDA=0 ./scripts/build-llama-cpp.sh
MSG
    exit 1
  fi
  say "CUDA found: $(nvcc --version | tail -1)"
fi

if [[ -d "$PREFIX/.git" ]]; then
  say "updating existing checkout at $PREFIX"
  git -C "$PREFIX" fetch --depth 1 origin master
  git -C "$PREFIX" reset --hard origin/master
else
  say "cloning llama.cpp into $PREFIX"
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$PREFIX"
fi

cd "$PREFIX"
CMAKE_FLAGS=(-B build -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON)
[[ "$USE_CUDA" == "1" ]] && CMAKE_FLAGS+=(-DGGML_CUDA=ON)

say "configuring"
cmake "${CMAKE_FLAGS[@]}"
say "building with $JOBS jobs (this takes a while)"
cmake --build build --config Release -j "$JOBS" --target llama-server llama-cli llama-quantize

BIN="$PREFIX/build/bin/llama-server"
[[ -x "$BIN" ]] || { echo "build finished but $BIN is missing" >&2; exit 1; }

say "built: $BIN"
"$BIN" --version 2>&1 | head -3 || true

# core/paths.py searches ~/.local/bin among other places, so a symlink there means
# Kamil finds it with no configuration at all.
mkdir -p "$HOME/.local/bin"
ln -sf "$BIN" "$HOME/.local/bin/llama-server"
ln -sf "$PREFIX/build/bin/llama-cli" "$HOME/.local/bin/llama-cli"
say "symlinked into ~/.local/bin (Kamil will find it automatically)"

cat <<MSG

Done. Verify Kamil sees it:
  python -m core.model --bin

If you keep llama.cpp somewhere unusual, set KAMIL_LLAMA_SERVER instead.
MSG
