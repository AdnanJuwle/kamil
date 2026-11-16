# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What Kamil is

Kamil is a **fully-local, offline AI system** built for one user (Adnan). The ambition is not "a chatbot" — it is meant to be *alive*: a persistent presence on the machine that remembers across sessions, knows who it's talking to (by voice), holds a stable identity and opinions, watches itself for drift, and can act on the computer (files, shell, tasks). "Kamil is the computer" — the long-term goal is for Kamil to *be* the interface to the machine, not an app you open.

Everything runs on local hardware via [llama.cpp](https://github.com/ggerganov/llama.cpp). **No external API calls, no telemetry, no data leaves the machine** — this is a hard design constraint, not a preference. Any change that introduces a cloud dependency is wrong by default.

This repo (`C:\kamil`) is the **Python backend**. The Electron desktop shell lives in a **sibling repo**, `C:\kamil-app`.

## Two repos

| Repo | Role |
|------|------|
| `C:\kamil` | Python backend: orchestrator, memory, indexing, voice, actions, FastAPI server. **This repo.** |
| `C:\kamil-app` | Electron shell: spawns the llama.cpp model servers + Python backend + eMCP bridge, system tray, `Ctrl+Space` global hotkey, loads the web UI from `localhost:3000`. |

## Running Kamil

Kamil is a **multi-process system**: several `llama-server.exe` instances (one per model role) + the Python FastAPI backend + an eMCP Node bridge. There are three ways to bring it up.

### 1. Electron app (the real entry point)
```bash
cd C:\kamil-app
npm install
npm start          # spawns ALL model servers + backend + emcp, then loads the UI
```
`src/index.js` orchestrates the whole startup: primary model first, voice model at +30s, backend at +5s, executor at +35s, polling `http://127.0.0.1:3000/` until the backend answers. Every child process auto-restarts on crash. `Ctrl+Space` toggles the window; closing minimizes to tray.

### 2. `start.bat` (backend-only, manual)
Launches just the primary + voice models and the web server. The council/executor models won't be running. Open `http://localhost:3000`.

### 3. Fully manual (for backend dev)
```bash
# Start whichever model servers you need (see "Models & ports" below), then:
C:\kamil\venv313\Scripts\activate
python run.py      # uvicorn on 0.0.0.0:3000
```

### 4. Fedora / Linux
```bash
./scripts/install-fedora.sh     # dnf deps + venv313 + webcam check
./scripts/build-llama-cpp.sh    # CURRENT llama.cpp (Huihui-Qwen3.8 needs arch 'qwen35')
./start.sh                      # one model + backend
./scripts/install-systemd.sh    # run at login, survive logout
```
See **`docs/FEDORA.md`** for the whole Linux story: paths, system control, the
room watcher, and the public-repo rules.

**Python: use `venv313` (Python 3.13).** Not `venv` (older, stale), and **not 3.14** — uvicorn + threading breaks on 3.14. The Electron app hard-codes `C:\kamil\venv313\Scripts\python.exe`.

**Dependencies** are pinned in `requirements.txt` (curated, direct deps) with a
fully-pinned `requirements.lock.txt` (complete `pip freeze` of venv313) for
reproducible installs. Dev/test tooling is in `requirements-dev.txt`. `ffmpeg`
must also be on PATH (webm→wav for transcription/enrollment).

### Tests
```bash
C:\kamil\venv313\Scripts\python.exe -m pip install -r requirements-dev.txt   # once
C:\kamil\venv313\Scripts\python.exe -m pytest          # full suite (offline, ~5s)
C:\kamil\venv313\Scripts\python.exe -m pytest --cov=memory --cov=core --cov=code13
```
The suite (`tests/`) runs **fully offline** — no llama servers, no models, no
ChromaDB. It monkeypatches each module's on-disk paths to temp dirs, so it never
touches real `data/` state. Covers the memory/drift logic (sanity, thread_state,
context_manager) and the sealed-succession crypto (seal, succession).

## Models & ports

**Kamil runs ONE model.** `Huihui-Qwen3.8-27B-abliterated` (a `qwen35`-architecture
27B) serves chat, voice AND vision. The old primary+voice pair and the separate
vision tier are gone: the model is multimodal, so the same server that holds the
conversation looks through the webcam.

| Role | Port | Resolved by | Notes |
|------|------|-------------|-------|
| **The model** | 8080 | `core/model.py` | chat + voice + vision, one process |
| Council advocate | 8081 | `core/council_loader.py` | on demand, devil's advocate |
| Council synthesizer | 8082 | `core/council_loader.py` | on demand, final synthesis |
| ~~Voice~~ | ~~8084~~ | — | **Retired** — the 27B answers directly, no handoff |
| ~~Vision~~ | ~~8098~~ | `core/vision_tier.py` | only spawned if the primary is NOT multimodal |
| Heavy | 8086 | `core/heavy_tier.py` | configured, on-demand coder tier |

**`core/model.py` is the single source of truth** for which GGUF and which
llama-server flags to use — the launchers (`start.sh`, `start.bat`, the systemd
units, the Electron shell) all ask it rather than hard-coding anything:

```bash
python -m core.model              # resolved weights, projector, VRAM fit
python -m core.model --serve-args # exact llama-server flags (via core/hwfit.py)
python -m core.model --bin        # the llama-server binary for this OS
```

It needs **two** files in `models/`: the weights (e.g.
`Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf`, ~16.8 GB) and the vision projector
`mmproj-model-bf16.gguf`. Projectors are model-specific; the resolver refuses a
mismatched pairing and reports text-only rather than garbling every frame.

**Paths and platform differences all resolve through `core/paths.py`** — the only
module allowed to name a drive letter. A test fails the build if any other module
does. Override with `KAMIL_ROOT`, `KAMIL_DATA_DIR`, `KAMIL_MODELS_DIR`,
`KAMIL_PROJECTS_ROOT`, `KAMIL_LLAMA_SERVER`, `KAMIL_PYTHON`, `KAMIL_FFMPEG`.

## Architecture

```
Electron shell (C:\kamil-app)
  ├─ spawns llama-server × N (ports 8080–8084)
  ├─ spawns eMCP bridge (Node, port 3001)
  └─ spawns Python backend ──► FastAPI :3000 (interfaces/web/app.py)
                                   │  serves web UI, SSE chat, voice, speaker, action endpoints
                                   ▼
                              Orchestrator (core/orchestrator.py)
                              builds a context-rich system prompt every turn, routes to a model
                                   │
        ┌──────────────┬──────────┴───────────┬─────────────────┬──────────────┐
        ▼              ▼                      ▼                 ▼              ▼
     Memory         Context/Index          Actions          Voice          Council
   (the "alive"   (code + domain +       (files/shell/    (Whisper STT +  (3-model
    core)          religious RAG)          tasks/eMCP)      Kokoro TTS +    deliberation)
                                                            speaker ID)
                          │
                          ▼
                  ChromaDB  +  SQLite  (data/)
```

### `core/` — orchestration
- `paths.py` — **the one place that knows where Kamil lives and what OS it is on.**
  Every absolute path and every platform branch (process kill, port freeing, shell,
  webcam backend, "what counts as the OS") resolves here. No other module may name a
  drive letter; `tests/test_paths.py` enforces that.
- `model.py` — **the one model.** Resolves which GGUF to serve, pairs it with its
  multimodal projector (strictly — a mismatched `mmproj` is refused), and computes the
  llama-server flags via `hwfit`. The launchers all call it; nothing hard-codes a model.
- `room_watch.py` — **the 24x7 webcam room monitor.** Owns the camera for the whole
  session, differences frames against a running average to find motion, records a clip
  per event to `data/room/`, logs it to `data/room_events.db`, and hands one keyframe to
  the vision model so the log reads in words. `MotionDetector` is split out so the maths
  is unit-tested offline with synthetic frames. Clips roll off after 14 days.
- `orchestrator.py` — the brain. Owns history, builds the per-turn system prompt (`_build_messages`), and exposes the chat modes:
  - `chat` / `chat_stream` — normal conversation (streamed via SSE).
  - `chat_agent` — **agentic tool-use loop** (multi-step: call tool → see result → continue). Streams events; mutating tools pause for confirmation. See `actions/agent.py`. This is the one tool-use path.
  - `chat_agentic` — code-aware path: indexes-on-demand and injects symbol context (note: single-shot, not the agent loop).
  - `council_chat` — three-model deliberation.
  - `chat_voice` / the `/voice_chat` endpoint — two-phase: small voice model answers directly OR says "let me check that for you" and hands off to the primary model.
  - `_build_messages` is where everything converges: identity, threads, long-term memories, the daily **schedule**, religious context, code/domain context, eMCP context all get layered into the system prompt. Read this method first when debugging "why did Kamil say X."
- `config.py` — URLs, defaults, loads `prompts/system.md` as `KAMIL_SYSTEM_PROMPT`.
- `council.py` / `council_loader.py` — Analyst (8080) → Devil's Advocate (8081) → Synthesizer (8082). The loader spawns the two extra servers on demand.
- `schedule.py` — Adnan's daily/weekly schedule as data: `current_block(now)`, `is_quiet_hours(now)`, `schedule_context()`. Single source of truth (the orchestrator and presence both read it).
- `presence.py` — **Kamil's heartbeat / proactive presence.** `evaluate(now, …)` is a PURE decision core: given the time, the active thread, due reminders, and cooldown/dedupe state, it returns at most one `ProactiveIntent` (a reminder, a dormant-thread check-in, or an off-by-default schedule nudge). Guarantees: due reminders bypass quiet hours/cooldown; everything else is suppressed during quiet hours and within a 45-min cooldown; every trigger de-dupes by key. Also `temporal_note()` (folds "it's been X since you last spoke" into reactive replies) and state persistence (`data/presence_state.json`). Delivery is the backend's job (see below).
- `reminders.py` — user-set reminders (`data/reminders.json`), backing the `remind` tool and fired by the heartbeat.

**The heartbeat** lives in `interfaces/web/app.py`: a daemon thread (`_heartbeat_loop`, every `KAMIL_HEARTBEAT_SECONDS`=60) gathers state, calls `presence.evaluate`, and — if something's worth saying — has the orchestrator phrase it in Kamil's voice (`orchestrator.proactive_message`, falls back to a template if the model is down) and queues it. The web UI polls `GET /presence` every 30s and renders queued lines as "kamil reached out" bubbles. Disable with `KAMIL_HEARTBEAT=0`.

### `memory/` — the "alive" core (most important subsystem)
This is what separates Kamil from a stateless chatbot. Treat it carefully.
- `context_manager.py` — **3-tier predictive context** (RAM = always injected / Cache = scored & promoted on relevance / ROM = ChromaDB cold storage). Replaces naive "summarize every N messages." Decision markers never fully evict.
- `long_term.py` — semantic long-term memory in ChromaDB (`kamil_memories` collection), retrieved by similarity each turn.
- `compressor.py` — compresses old message windows into stored memories (gated by the sanity check).
- `sanity.py` — **drift monitoring**. Validates compressed memories, tracks response-distribution shift and user-correction frequency. If drift exceeds threshold it *auto-disables learning* (stops writing new memories) until reviewed. `data/drift_state.json`, `data/sanity_log.json`.
- `identity.py` — what Kamil knows about the user (`data/identity.json`).
- `thread_state.py` / `threads.py` — the live "what are we actively working on" document. Survives compression and restarts; archived (`data/threads_archive/`) only when explicitly closed.
- `speaker.py` — **voice-based speaker recognition** (resemblyzer 256-dim voiceprints, cosine match). Gates trust: `owner` (Adnan, full context) / `trusted` (no owner-private context) / `guest` (general only). `data/speakers.json`.
- `store.py` — SQLite (`data/sessions.db`) for history + settings.

### `context/` — retrieval / RAG
- `indexer.py` — project code indexer; lazy (scans project structure on selection, embeds on demand). Self-healing ChromaDB cache (`data/indexed_projects.json`).
- `symbol_indexer.py` — exact symbol lookup (classes, methods, module symbols, file-reference detection) — the precise code-context path used by the agentic mode.
- `embedder.py` — `all-MiniLM-L6-v2` sentence-transformers + shared ChromaDB client.
- `domain_indexer.py` — ~29k chunks across 6 domains (`data/domain_books/`: cognitive, kamil_arch, mlops, prana, reliability, security). Domain knowledge injected when relevant.
- `religious_indexer.py` — Quran + Sahih al-Bukhari RAG (`data/religious/`); quoted verbatim, never paraphrased/fabricated.
- `watcher.py` — filesystem watcher → auto re-index on change.

### `actions/` — doing things on the machine
**One tool path.** (The older XML `dispatcher.py` system + its `filesystem.py`/`shell.py` backends and the `run_executor` executor-model path were retired in 2026-06.)
- `tools.py` — **the canonical typed tool registry** (read_file, write_file, edit_file, list_directory, shell, search_files, create_task, system_info). Each tool has a permission (`auto` runs immediately, `confirm` needs approval). Guardrails: writes/edits are fenced to `KAMIL_WRITE_ROOTS` (default workspace + `repos/`); shell has a destructive-command denylist; `edit_file` is a surgical unique-snippet replace. `parse_tool_calls` is tolerant (```tool / ```json / bare JSON).
- `agent.py` — **the agentic tool-use loop** (`iter_agent`). After each model turn it runs auto tools and feeds results back, so the model can read → edit → run-test across multiple steps. Pauses with `awaiting_confirmation` (resumable) the moment a mutating tool is requested. Pure/injectable — fully unit-tested offline with a fake model.
- **System control** (Linux default, `KAMIL_SYSTEM_CONTROL`): on Fedora Kamil owns the
  machine — the write-root fence lifts, sudo/systemd/packages are in reach, and shell may
  edit files. What survives is a *destructive-command gate*: `rm -rf`, `dd`, `mkfs`,
  `poweroff`, `userdel`, `git push --force` and friends are no longer refused but PAUSE
  for approval (privilege wrappers like `sudo`/`env VAR=x` are stripped before deciding).
  Three typed tools back it: `service_control`, `package_manager`, `process_control` —
  read-only verbs run immediately, state-changing ones ask. Secret reads stay blocked in
  both modes; that is a design guarantee, not part of the sandbox.
- `room_history` tool — what the camera has seen, so "was anyone here?" is answerable.
- `tasks.py` — SQLite task store (`data/tasks.db`), backing the `create_task` tool.
- `emcp_client.py` — bridge to the eMCP server (`C:\kamil-app`, port 3001): turns raw MCP tool responses into typed, cross-linked context nodes injected into the prompt.

The orchestrator exposes the loop as `chat_agent()` (SSE endpoint `POST /chat_agent`), which streams structured events: `text`, `tool_call`, `tool_result`, `awaiting_confirmation`, `done`.

### `interfaces/web/app.py` — FastAPI server (port 3000)
Single-file server: serves the UI (`index.html`), SSE chat endpoints (`/chat`, `/chat_actions`, `/chat_agentic`), `/council`, voice (`/voice_chat`, `/transcribe`, `/synthesize`), speaker management (`/speakers/*`), identity/threads, drift reporting (`/drift`), and debug routes (`/debug_full_prompt` is the fastest way to see the exact system prompt). **All heavy init (Orchestrator, Whisper, Indexer) happens synchronously at import time, before uvicorn starts** — first boot is slow (30–60s) by design, to dodge 3.14 threading issues.

### Voice pipeline
Whisper (`small`, CPU) for STT → speaker ID on the same audio → voice model for a fast reply or handoff → Kokoro ONNX TTS (`am_michael` voice, `voices/kokoro-v1.0.onnx`). `ffmpeg` is required on PATH for webm→wav conversion in enrollment/transcription.

## Data layout (`data/`) — local state, ALL gitignored

| Path | What |
|------|------|
| `chroma/` | ChromaDB vector store (code, memories, domain, religious) |
| `sessions.db` | history + settings (SQLite) |
| `tasks.db` | tasks (SQLite) |
| `identity.json` | what Kamil knows about the user |
| `speakers.json` | enrolled voiceprints + trust levels |
| `active_thread.json`, `threads_archive/` | live + closed work threads |
| `drift_state.json`, `sanity_log.json` | drift monitor state |
| `domain_books/`, `religious/` | source docs for the RAG indexes |
| `projects/` | cached project scans/architecture |
| `room/` | webcam clips from the room watcher (`.mp4`, 14-day retention) |
| `room_events.db` | motion events + their model-written descriptions |
| `logs/` | `llama.log`, `backend.log` (start.sh / systemd) |

**All of `data/` is gitignored, and the repo is PUBLIC.** It holds identity,
voiceprints, conversation history, drift metrics, indexed copies of private repos and
webcam footage. None of it is ever committed.

> `.gitignore` was once **corrupted** — a UTF-16 block pasted into the middle made git
> treat the whole file as binary and silently ignore every rule, which is how
> `kamil.key`, `data/identity.json` and `data/speakers.json` came to be tracked. They
> have since been purged from history and the key rotated. Keep that file plain UTF-8:
> `file .gitignore` must say `UTF-8 text`.

## Conventions & gotchas

- **Offline is non-negotiable.** `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1` are set at the top of `app.py`. Don't add code that phones home.
- **Identity lives in `prompts/system.md`.** Kamil has a deliberate personality (direct, low-filter, Islamic ethical default, treats the user as a peer/friend). When changing tone/behavior, edit the prompt — don't hard-code personality in Python. Voice mode has its own inline system prompt in `app.py::voice_chat`.
- **Paths are NOT hard-coded any more.** Everything goes through `core/paths.py`; a test
  (`test_no_literal_windows_paths_in_source`) fails the build if a module names a drive
  letter. When you need a path, ask that module — never write `C:\...` or a leading `/`,
  and never join with `\` or `/` by hand.
- **One model, resolved not hard-coded.** Ask `core/model.py` for the weights and flags.
  `config.SINGLE_MODEL` is true when voice and reasoning share a server, which is the
  normal case — code that used to orchestrate the two-stage voice handoff checks it.
- **The orchestrator file has stale comments** (e.g. references to a removed "banter" system, a `# ... rest unchanged` note). Trust the code, not the comments.
- **Several chat modes** route differently — when a request misbehaves, first identify which endpoint/mode handled it (`/chat` vs `/chat_actions` vs `/chat_agentic` vs `/voice_chat`).
- **Tests live in `tests/`** (pytest, offline — see the Tests section above). The old ad-hoc `test_*.py` scripts at the repo root (`test_client.py`, `test_indexer.py`, `test_parser.py`) and `test_*.wav` fixtures are scratch, not part of the suite. **Run `python -m pytest` after changing memory/drift or succession logic.**
- **Council loading is heavy** (spawns two more 4–8B models, ~120s cold start). It's on-demand and stays warm for consecutive deliberations, then an idle timer in `council_loader.py` frees the VRAM after `KAMIL_COUNCIL_IDLE` seconds (default 600). `atexit` also unloads it on backend shutdown.

## State of the project (assessment, 2026-06)

**Working:** local multi-model serving, streaming chat, the layered memory system (3-tier context, long-term, identity, threads, drift), voice in/out with speaker recognition, code + domain + religious RAG, file/shell/task actions with confirmation gating, the Electron shell with auto-spawn/restart and tray.

**Foundation work — DONE (2026-06):**
- ✅ `requirements.txt` + `requirements.lock.txt` + `requirements-dev.txt` — env is now reproducible.
- ✅ Offline pytest suite in `tests/` covering memory/drift + succession crypto.
- ✅ Council VRAM leak fixed — idle-unload timer + `atexit` cleanup.

**Agency sharpening — DONE (2026-06):**
- ✅ Real agentic loop (`actions/agent.py`) — tools chain across steps with results fed back.
- ✅ Tool guardrails — write-root fencing + shell denylist + surgical `edit_file`; fixed the broken `create_task` tool.
- ✅ Tolerant tool-call parsing; `POST /chat_agent` SSE endpoint; 23 new offline tests.

**Agent confirmation resume — DONE (2026-06):** `/agent <task>` in the web UI runs
the loop, renders tool calls/results inline, and drives approve→resume through the
confirm panel. Backend: `POST /agent_resume` + a server-side `_agent_runs` registry
(the transcript stays server-side, keyed by `run_id`). `orchestrator.resume_agent`
continues the loop after approved tools run.

**XML action system — RETIRED (2026-06):** one tool path now (`tools.py`/`agent.py`).
Removed `actions/dispatcher.py`, `filesystem.py`, `shell.py`, `run_executor`,
`_executor_prompt`, `chat_with_actions`, the `/execute_action` + `/chat_actions`
endpoints, the dead UI confirm path, and the executor model (Electron no longer
spawns port 8083). `EXECUTOR_SERVER_URL` dropped from config.

**Cross-platform + single model + room watch — DONE (2026-11):**
- ✅ `core/paths.py` — every absolute path and platform branch resolves through one
  module. ~50 hard-coded `C:\` sites ported; a test fails the build on any new one.
  Fixed three real bugs this exposed (the project-scan cache dir in `indexer.py` and
  `symbol_indexer.py`, and `_self_source_context`'s root, all pinned to `C:\kamil`).
- ✅ One model (`core/model.py`): `Huihui-Qwen3.8-27B-abliterated` serves chat, voice
  and vision. Voice handoff retired (`config.SINGLE_MODEL`); `vision_tier` short-circuits
  to the primary when it is multimodal, so nothing extra is spawned. Strict `mmproj`
  pairing — a projector from another model/size is refused rather than trusted.
- ✅ Full system control on Linux with a destructive-command gate; `service_control`,
  `package_manager`, `process_control` tools.
- ✅ `core/room_watch.py` — 24x7 webcam monitoring, motion-triggered clips, model-written
  descriptions, 14-day retention. Owns the camera so `look` still works.
- ✅ Fedora path: `scripts/install-fedora.sh`, `scripts/build-llama-cpp.sh`,
  `scripts/install-systemd.sh`, `systemd/*.service`, `start.sh`, `docs/FEDORA.md`.
- ✅ Electron shell made cross-platform; it no longer serves llama on `0.0.0.0`.
- ✅ Test suite 135 → 252, all offline. New: `test_paths.py`, `test_model.py`,
  `test_room_watch.py`, `test_system_control.py`.
- ✅ Public-repo cleanup: `.gitignore` de-corrupted; the private key, voiceprints,
  identity, thread history and copyrighted books purged from all history.

**Rough edges / still outstanding:**
1. **`_agent_runs` is in-memory** — a paused agent run is lost if the backend restarts before approval (acceptable; runs are short-lived).
2. **`requirements.lock.txt` was frozen on Windows** — `install-fedora.sh` falls back to
   `requirements.txt` when it will not resolve on Linux. A Linux lock file is worth adding.
3. **Dead/unused config** — `HEAVY_SERVER_URL` (8086), the older `venv`, several unrouted GGUFs.
4. **The destructive-command gate is a heuristic** over command text, not a security
   boundary. It stops accidents, not determined mistakes — scope passwordless sudo narrowly.
5. **Room clips are unencrypted** `.mp4` on disk. Put `data/` on an encrypted filesystem
   if that matters, and tell anyone else who uses the room that the camera is on.

When in doubt about hardware/model specifics or the intended behavior, the git history (`git log`) is descriptive and the `*.md` design docs at the repo root (`PROJECT_SUMMARY.md`, `CONTEXT_MANAGER_INTEGRATION.md`, `BUILD_REPORT.md`, etc.) capture the design intent — though some predate the current code and may have drifted.
