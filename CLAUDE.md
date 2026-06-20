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

Models are `*.gguf` files in `C:\kamil\models\` (gitignored, large). Each role is a separate `llama-server` process. `llama-server.exe` is expected at `C:\llama.cpp\build\bin\llama-server.exe`.

| Role | Port | Model (default) | Started by | Notes |
|------|------|-----------------|-----------|-------|
| **Primary** | 8080 | `deepseek-r1-distill-14b-q4` | Electron / start.bat | Main reasoning + the Council's "Analyst" |
| **Council advocate** | 8081 | `dolphin-mistral-7b-q4` | `core/council_loader.py` on demand | Devil's advocate |
| **Council synthesizer** | 8082 | `llama-3.1-8b-instruct-q4` | `core/council_loader.py` on demand | Final synthesis |
| **Executor** | 8083 | `qwen2.5-3b-executor-q8` | Electron (+35s) | Fast action/tool generation |
| **Voice** | 8084 | `gemma4-e4b-q4` | Electron (+30s) | Quick spoken replies + handoff decision |
| **Heavy** | 8086 | — | (configured, currently unused) | `HEAVY_SERVER_URL` reserved |

Ports/URLs are overridable via env vars read in `core/config.py` (`KAMIL_SERVER_URL`, `KAMIL_EXECUTOR_URL`, `KAMIL_VOICE_URL`, `KAMIL_HEAVY_URL`, `KAMIL_TIMEOUT`, `KAMIL_TEMPERATURE`, `KAMIL_MAX_TOKENS`). The `models/` dir holds several extra GGUFs (qwen-coder, qwen-32b, claude-distills) not wired into the default routing — they're available for experimentation.

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
- `orchestrator.py` — the brain. Owns history, builds the per-turn system prompt (`_build_messages`), and exposes the chat modes:
  - `chat` / `chat_stream` — normal conversation (streamed via SSE).
  - `chat_with_actions` — model emits tool-call JSON; auto-runs safe tools, queues risky ones for confirmation.
  - `chat_agent` — **agentic tool-use loop** (multi-step: call tool → see result → continue). Streams events; mutating tools pause for confirmation. See `actions/agent.py`.
  - `chat_agentic` — code-aware path: indexes-on-demand and injects symbol context (note: single-shot, not the agent loop).
  - `council_chat` — three-model deliberation.
  - `chat_voice` / the `/voice_chat` endpoint — two-phase: small voice model answers directly OR says "let me check that for you" and hands off to the primary model.
  - `run_executor` — routes to the small executor model for direct file/shell actions.
  - `_build_messages` is where everything converges: identity, threads, long-term memories, the daily **schedule**, religious context, code/domain context, eMCP context all get layered into the system prompt. Read this method first when debugging "why did Kamil say X."
- `config.py` — URLs, defaults, loads `prompts/system.md` as `KAMIL_SYSTEM_PROMPT`.
- `council.py` / `council_loader.py` — Analyst (8080) → Devil's Advocate (8081) → Synthesizer (8082). The loader spawns the two extra servers on demand.

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
- `tools.py` — **the canonical typed tool registry** (read_file, write_file, edit_file, list_directory, shell, search_files, create_task, system_info). Each tool has a permission (`auto` runs immediately, `confirm` needs approval). Guardrails: writes/edits are fenced to `KAMIL_WRITE_ROOTS` (default workspace + `repos/`); shell has a destructive-command denylist; `edit_file` is a surgical unique-snippet replace. `parse_tool_calls` is tolerant (```tool / ```json / bare JSON).
- `agent.py` — **the agentic tool-use loop** (`iter_agent`). After each model turn it runs auto tools and feeds results back, so the model can read → edit → run-test across multiple steps. Pauses with `awaiting_confirmation` (resumable) the moment a mutating tool is requested. Pure/injectable — fully unit-tested offline with a fake model.
- `dispatcher.py` — the **older** XML-tag action system (`<action type="write_file" .../>`), still used by the orchestrator's `run_executor`. Prefer `tools.py` + `agent.py` for new work; this remains for the small executor-model path.
- Backends for the XML path: `filesystem.py`, `shell.py`, `tasks.py` (SQLite `data/tasks.db`).
- `emcp_client.py` — bridge to the eMCP server (`C:\kamil-app`, port 3001): turns raw MCP tool responses into typed, cross-linked context nodes injected into the prompt.

The orchestrator exposes the loop as `chat_agent()` (SSE endpoint `POST /chat_agent`), which streams structured events: `text`, `tool_call`, `tool_result`, `awaiting_confirmation`, `done`.

### `interfaces/web/app.py` — FastAPI server (port 3000)
Single-file server: serves the UI (`index.html`), SSE chat endpoints (`/chat`, `/chat_actions`, `/chat_agentic`), `/council`, voice (`/voice_chat`, `/transcribe`, `/synthesize`), speaker management (`/speakers/*`), identity/threads, drift reporting (`/drift`), and debug routes (`/debug_full_prompt` is the fastest way to see the exact system prompt). **All heavy init (Orchestrator, Whisper, Indexer) happens synchronously at import time, before uvicorn starts** — first boot is slow (30–60s) by design, to dodge 3.14 threading issues.

### Voice pipeline
Whisper (`small`, CPU) for STT → speaker ID on the same audio → voice model for a fast reply or handoff → Kokoro ONNX TTS (`am_michael` voice, `voices/kokoro-v1.0.onnx`). `ffmpeg` is required on PATH for webm→wav conversion in enrollment/transcription.

## Data layout (`data/`) — local state, not all gitignored

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

Some of this contains **personal data** — past commits (`64ce172`, `ac9ab25`) cleaned sensitive files out of tracking. Be careful what you add to git; check `.gitignore` before committing anything under `data/`.

## Conventions & gotchas

- **Offline is non-negotiable.** `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1` are set at the top of `app.py`. Don't add code that phones home.
- **Identity lives in `prompts/system.md`.** Kamil has a deliberate personality (direct, low-filter, Islamic ethical default, treats the user as a peer/friend). When changing tone/behavior, edit the prompt — don't hard-code personality in Python. Voice mode has its own inline system prompt in `app.py::voice_chat`.
- **Paths are hard-coded to `C:\kamil`** in many places (`run.py`, `thread_state.py`, `sanity.py`, model paths, the Electron shell). This is a single-machine personal system; don't assume portability without auditing the absolute paths.
- **The orchestrator file has stale comments** (e.g. references to a removed "banter" system, a `# ... rest unchanged` note). Trust the code, not the comments.
- **Two action systems and several chat modes** route differently — when a request misbehaves, first identify which endpoint/mode handled it (`/chat` vs `/chat_actions` vs `/chat_agentic` vs `/voice_chat`).
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

**Rough edges / still outstanding:**
1. **Two action systems still coexist** — `tools.py`/`agent.py` (canonical) and the older XML `dispatcher.py` (used only by `run_executor`). Consider retiring the XML path.
2. **Confirmation resume isn't wired end-to-end in the UI yet** — `agent.py` returns a resumable transcript + `resume_agent`, but the web frontend doesn't drive the approve→resume round-trip yet.
3. **Hard-coded `C:\kamil` paths** throughout — fine for the single machine, but fragile.
4. **Dead/unused config** — `HEAVY_SERVER_URL` (8086), the older `venv`, several unrouted GGUFs.
5. **Stale comments** in `orchestrator.py` from removed features.

When in doubt about hardware/model specifics or the intended behavior, the git history (`git log`) is descriptive and the `*.md` design docs at the repo root (`PROJECT_SUMMARY.md`, `CONTEXT_MANAGER_INTEGRATION.md`, `BUILD_REPORT.md`, etc.) capture the design intent — though some predate the current code and may have drifted.
