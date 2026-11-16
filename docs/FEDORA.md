# Kamil on Fedora

Kamil was born on one Windows box with its paths written into ~50 source files.
This document covers what changed to make it portable, and the two decisions on
this machine that are deliberately riskier than the Windows setup: **Kamil owns
the system**, and **the camera is always on**.

---

## 1. What made it portable

Every absolute path now resolves through **`core/paths.py`**. That module is the
only place in the tree allowed to name a drive letter, and a test
(`tests/test_paths.py::test_no_literal_windows_paths_in_source`) fails the build
if any other module does.

Resolution is always: **env var → platform-conventional location → fallback**.

| What | Env override | Default on Fedora |
|---|---|---|
| Kamil's source root | `KAMIL_ROOT` | the parent of `core/` |
| Local state | `KAMIL_DATA_DIR` | `<root>/data` |
| Models | `KAMIL_MODELS_DIR` | `<root>/models` |
| Your editable projects | `KAMIL_PROJECTS_ROOT` | first of `~/repos`, `~/Documents/repos`, `~/Projects`, `~/src`, `~/dev` that exists |
| `llama-server` | `KAMIL_LLAMA_SERVER` | `$PATH`, then `/usr/local/bin`, `/opt/llama.cpp/build/bin`, `~/.local/bin`, `~/llama.cpp/build/bin` |
| `ffmpeg` | `KAMIL_FFMPEG` | `$PATH` |
| Python | `KAMIL_PYTHON` | `<root>/venv313/bin/python3` |

Platform differences that used to be Windows-only are now branched properly:
process-tree kills (`taskkill /T` vs `pkill -P`), freeing a stale model port
(`Get-NetTCPConnection` vs `fuser -k`), the shell `subprocess` uses (`cmd` vs
`$SHELL`), the webcam backend (DirectShow vs V4L2), and what counts as "the
operating system" for the tool guardrails.

---

## 2. One model instead of two

The old setup ran a primary model on `:8080` and a small Gemma voice model on
`:8084`, with a `"let me check that for you"` handoff between them. That existed
only because the voice model was a 4B — fast but too dim to answer anything real.

Now there is **one model**: `Huihui-Qwen3.8-27B-abliterated`. It is strong enough
to answer spoken questions directly, so the handoff is gone (`config.SINGLE_MODEL`
makes `chat_voice` answer in one pass instead of calling the same weights twice).
It is also **multimodal**, which is what makes the room watcher affordable: the
same server holding the conversation can look at a webcam frame, so there is no
second model on the GPU and no load/unload churn per motion event
(`core/vision_tier.py` short-circuits to the primary when it can see).

**`core/model.py` is the only place that decides which weights and flags to use.**
`start.sh`, `start.bat`, the systemd units and the Electron shell all ask it:

```bash
python -m core.model              # what Kamil resolved, and how it fits this GPU
python -m core.model --serve-args # the exact llama-server flags
python -m core.model --bin        # the llama-server binary
```

### Getting the model

```bash
mkdir -p ~/kamil/models && cd ~/kamil/models
```

From `huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF`, download **two** files:

| File | Size | Why |
|---|---|---|
| `Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf` | ~16.8 GB | the weights; fits a 24 GB card with room for KV cache |
| `mmproj-model-bf16.gguf` | small | the vision projector — **without it Kamil is text-only and the room watcher cannot describe what it sees** |

Other quants: `Q6_K` ~22.4 GB, `Q8_0` ~29 GB (needs offload on a 24 GB card —
`core/hwfit.py` works the flags out for you). Avoid the `Ternary`/`Bonsai`
variants: the model card says stock llama.cpp will not run them, and the resolver
deliberately scores them last.

> **Projectors are not interchangeable.** An `mmproj` built for Qwen3-VL-8B will
> not work with these 27B weights. `core/model.py` refuses a mismatched pairing and
> reports text-only rather than feeding the model garbage — a generically-named
> projector (`mmproj-model-*.gguf`) is assumed to pair with whatever single model is
> served, but a family-named one must agree on family *and* parameter size.

### llama.cpp must be current

The model reports its architecture as **`qwen35`**, and its vision path needs
llama.cpp's `mtmd` support. A build from before that landed will refuse to load the
GGUF outright. The model card says plainly: *use the latest llama.cpp*.

```bash
./scripts/build-llama-cpp.sh         # CUDA build into ~/llama.cpp, symlinks ~/.local/bin
LLAMA_CUDA=0 ./scripts/build-llama-cpp.sh   # CPU-only (a 27B will crawl)
```

---

## 3. Install

```bash
git clone https://github.com/AdnanJuwle/kamil.git ~/kamil
cd ~/kamil
./scripts/install-fedora.sh      # dnf packages + venv313 + webcam check
./scripts/build-llama-cpp.sh     # current llama.cpp with CUDA
# ... download the two model files into models/ ...
./start.sh
```

Run it at login (and keep it running when you log out):

```bash
./scripts/install-systemd.sh
systemctl --user start kamil-llama kamil
journalctl --user -u kamil-llama -f
```

User units, not system units — Kamil needs your GPU, webcam and home directory.
`install-systemd.sh` runs `loginctl enable-linger`, which is what makes "24x7"
actually true.

### Python version

Use **3.13**. Not 3.14 — uvicorn + threading breaks on it. `install-fedora.sh`
installs `python3.13` and builds `venv313` from it.

### Webcam permission

The watcher needs your user in the `video` group:

```bash
sudo usermod -aG video "$USER"    # then log out and back in
v4l2-ctl --list-devices           # confirm the camera is visible
```

---

## 4. Kamil owns this machine

On Fedora, `KAMIL_SYSTEM_CONTROL` defaults to **on**. On Windows/macOS it defaults
to off. What changes:

* **The write-root fence lifts.** Kamil can write anywhere on the filesystem,
  including its own source. Previously writes were confined to the projects root.
* **`sudo` is in reach**, as are systemd, package management and process control,
  through the `shell` tool and three typed tools: `service_control`,
  `package_manager`, `process_control`.
* **Shell may edit files** (`sed -i`, `tee`, redirects). Administering a box means
  editing its config; forcing that through `write_file` would only get in the way.

### What still stops it

**Destructive commands pause for your approval.** They are not refused — Kamil can
run them once you say yes — but it cannot run them unattended:

* data loss: `rm`, `shred`, `wipefs`, recursive deletes
* disks: `dd`, `mkfs`, `fdisk`, `parted`, anything writing to `/dev/sd*` or `/dev/nvme*`
* machine state: `shutdown`, `poweroff`, `reboot`, `systemctl isolate`/`mask`
* accounts: `userdel`, `usermod`, `passwd`
* package removals that can gut the system: `dnf remove -y`, `rpm -e`, `pacman -Rns`
* history destruction: `git push --force`, `git reset --hard`, `filter-repo`

The gate strips privilege wrappers before deciding, so `sudo rm -rf /` and
`env FOO=1 sudo -n poweroff` are caught the same as the bare commands.
Read-only calls to the typed tools (`status`, `search`, `list`) run without
pausing; state-changing ones ask.

**Secrets remain unreadable** — `.env`, `*.key`, `credentials*`, and the sealed
`code13/` succession failsafe. That block is one of Kamil's own design guarantees,
not part of the sandbox, so system control does not lift it.

To run fully fenced on this machine anyway:

```bash
KAMIL_SYSTEM_CONTROL=0 ./start.sh
```

### Passwordless sudo

The typed tools call `sudo -n` (non-interactive) — there is no TTY for a password
prompt, so without a sudoers rule those calls fail with a clear sudo error and
nothing else breaks. If you want them to work:

```bash
# /etc/sudoers.d/kamil  (edit with: sudo visudo -f /etc/sudoers.d/kamil)
adnan ALL=(root) NOPASSWD: /usr/bin/systemctl, /usr/bin/dnf
```

> Scope it to the commands you actually want, as above. A blanket
> `NOPASSWD: ALL` hands unattended root to a language model, and the destructive
> gate is a heuristic over command text, not a security boundary — it is there to
> stop an accident, not a determined mistake.

---

## 5. The room watcher

`core/room_watch.py`. On Fedora `KAMIL_ROOM_WATCH` defaults to **on**: the camera
opens with the backend and stays open for the whole session.

**How it works.** Frames are read at ~6 fps and differenced against a running
average at reduced resolution — a few hundred microseconds per frame, no model
involved. When a region larger than `KAMIL_ROOM_MIN_AREA` changes, an event opens:
a clip starts recording to `data/room/`, a row goes into `data/room_events.db`, and
one keyframe is handed to the vision model to describe in words. The clip keeps
rolling until the room has been still for `KAMIL_ROOM_CLIP_TAIL` seconds.

Three things keep it out of the way:

* **It owns the camera.** A V4L2 device generally cannot be opened twice, so the
  `look` tool and `/room/frame` read the watcher's newest frame rather than fighting
  it for the device.
* **Inference never blocks capture.** Describing a frame with a 27B model takes
  seconds, so keyframes go to a worker through a depth-1 slot and are dropped if it
  is busy. At most one description per `KAMIL_ROOM_VISION_COOLDOWN` (45s default).
* **It forgets.** Clips older than `KAMIL_ROOM_RETENTION_DAYS` (14) are deleted
  hourly, along with their rows, so an always-on camera cannot quietly fill the disk.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KAMIL_ROOM_WATCH` | `1` on Linux | master switch |
| `KAMIL_CAMERA_INDEX` | `0` | which `/dev/video*` |
| `KAMIL_ROOM_FPS` | `6` | capture/analysis rate |
| `KAMIL_ROOM_MIN_AREA` | `1200` | motion sensitivity (px² at 480px wide) |
| `KAMIL_ROOM_THRESHOLD` | `22` | per-pixel change threshold |
| `KAMIL_ROOM_CLIP_TAIL` | `4` | seconds of stillness before a clip closes |
| `KAMIL_ROOM_MAX_CLIP` | `120` | seconds before a long event is split |
| `KAMIL_ROOM_MIN_EVENT` | `0.6` | shorter blips are discarded with their clip |
| `KAMIL_ROOM_VISION_COOLDOWN` | `45` | seconds between model descriptions |
| `KAMIL_ROOM_RETENTION_DAYS` | `14` | `0` disables purging |
| `KAMIL_ROOM_DIR` | `data/room` | where clips go |
| `KAMIL_ROOM_PROMPT_HOURS` | `8` | how much room history reaches the prompt |

### Using it

```bash
curl -s localhost:3000/room/status | jq        # is the eye open, disk used
curl -s 'localhost:3000/room/events?hours=24'  # the motion log
curl -s localhost:3000/room/frame -o now.jpg   # newest frame
curl -XPOST localhost:3000/room/control -d '{"action":"stop"}'
```

Kamil reaches the same history through the **`room_history`** tool, and a short
digest of recent activity is folded into its prompt, so "was anyone here while I
was out?" is answerable. Turn the camera off for a while with
`/room/control {"action":"stop"}` — nothing else degrades.

### Before you leave it running

Two things worth being deliberate about, because the code cannot decide them:

* **Other people.** A camera recording a room records everyone who walks into it,
  and recording identifiable people without their knowledge is regulated in many
  places. If anyone other than you uses this room, tell them.
* **The footage is not encrypted at rest.** `data/room/` is plain `.mp4` on disk,
  gitignored but readable by anything running as your user. If that matters, put
  `data/` on an encrypted filesystem.

---

## 6. Public repository

This repo is public. Two consequences:

**Nothing under `data/` is ever committed.** `.gitignore` excludes all of it —
identity, voiceprints, conversation history, drift metrics, indexed copies of your
private repos, and webcam footage. The same goes for `models/*.gguf`,
`voices/*`, `workspace/` and every `*.key` / `*.crt`.

> `.gitignore` was previously **corrupted**: a UTF-16 block had been pasted into the
> middle of it, so git treated the file as binary and silently ignored every rule.
> That is how `kamil.key`, `data/identity.json` and `data/speakers.json` ended up
> tracked. Keep that file plain UTF-8 — `file .gitignore` should say `UTF-8 text`.

### The TLS pair

`kamil.key` / `kamil.crt` were committed while the repo was public, so that key is
compromised. It has been replaced:

```bash
./scripts/make-cert.sh              # loopback pair (localhost, 127.0.0.1, ::1), 2 years
HOSTS="localhost,kamil.local" ./scripts/make-cert.sh
```

The old cert was `CN=192.168.1.7` — a LAN IP, from when Kamil served HTTPS to other
devices on the network. The replacement is deliberately **loopback-only**, matching
the current trust boundary rather than rebuilding the old exposure. (That old cert
also published your LAN address, which is minor but was avoidable.)

Note that **nothing currently loads either file** — `uvicorn.run` takes no `ssl_*`
arguments, so Kamil speaks plain HTTP on 127.0.0.1. The pair is precautionary. To
actually serve HTTPS:

```python
uvicorn.run(app, host=host, port=port,
            ssl_keyfile="kamil.key", ssl_certfile="kamil.crt")
```

`.gitignore` excludes `*.key`, `*.crt` **and `*.replaced-*`** — that last one
matters, because a rotation backup named `kamil.key.replaced-20261116` slips past
`*.key` and would leave a retired private key one `git add -A` from a public push.

**`run.py` binds loopback only** and the backend requires `KAMIL_API_TOKEN`.
127.0.0.1 is not a trust boundary on a shared machine, and the backend exposes
shell and file tools with system control on. Do not set `KAMIL_BIND_HOST=0.0.0.0`.
The Electron shell generates a fresh token per launch; `start.sh` and the systemd
units inherit whatever is in the environment.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `llama-server not found` | run `scripts/build-llama-cpp.sh`, or set `KAMIL_LLAMA_SERVER` |
| model refuses to load, unknown architecture | llama.cpp predates `qwen35` — rebuild it |
| `python -m core.model` says "no usable GGUF" | weights are not in `models/`, or `KAMIL_MODEL` points nowhere |
| vision unavailable, model present | the `mmproj` is missing or mismatched — check `python -m core.model` |
| `cannot open camera 0` | not in the `video` group, wrong `KAMIL_CAMERA_INDEX`, or the device is in use |
| `VideoWriter failed (codec missing?)` | opencv built without the `mp4v` muxer; try `KAMIL_ROOM_FOURCC=XVID` |
| `look` says the watcher has no recent frame | the camera dropped; `/room/status` carries the error |
| `service_control` fails with a sudo error | no passwordless sudo rule (see §4) |
| tools refuse system work | `KAMIL_SYSTEM_CONTROL` is off — check `/room/status` sibling `system_info` |
| backend slow to start | by design: Whisper, embeddings and Chroma load before uvicorn |

```bash
python -m core.model                 # which weights, which projector, how it fits
python -c "from core import paths; print(paths.SYSTEM, paths.KAMIL_ROOT, paths.llama_server_bin())"
python -c "from actions import tools; print('system control:', tools.system_control())"
python -c "from core import room_watch as r; print(r.status())"
python -m pytest -q                  # 252 offline tests, no models or camera needed
```
