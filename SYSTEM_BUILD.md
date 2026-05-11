# Kamil System — Complete Build Summary

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│         Kamil Electron App (kamil-app/)         │
│  • Window management, tray, hotkeys (main.js)   │
│  • Secure IPC bridge (preload.js)               │
│  • Starts/monitors services                     │
└────────────┬────────────────────────────────────┘
             │ https://localhost:3000
             ▼
┌─────────────────────────────────────────────────┐
│    Kamil Backend (interfaces/web/app.py)        │
│  • FastAPI web server, streaming responses      │
│  • Voice: /synthesize, /voice_chat, /transcribe│
│  • Actions: /chat_actions with pending approval│
│  • Project indexing & semantic search           │
│  • Intent classification (self/code/general)    │
└────────────┬────────────────────────────────────┘
             │
      ┌──────┴──────┬──────────┬──────────┐
      ▼             ▼          ▼          ▼
  ┌────────┐  ┌────────┐ ┌────────┐ ┌──────────┐
  │Orchestr│  │Indexer │ │MemLong │ │ Executor │
  │-ator   │  │ +Cache │ │ -Term  │ │ (Shell)  │
  └────────┘  └────────┘ └────────┘ └──────────┘
      │            │           │
      └────────────┴───┬───────┘
                       ▼
              ┌──────────────────┐
              │  ChromaDB        │
              │ (Persistent)     │
              │ (Shared Client)  │
              └──────────────────┘
```

## Core Components

### 1. **Electron Desktop App** (`kamil-app/`)

**Files:**
- `src/main.js` — Main process, spawns services, tray management
- `src/preload.js` — Secure IPC bridge to renderer
- `package.json` — Dependencies and scripts
- `forge.config.js` — Squirrel installer config

**Features:**
- Manages llama-server + Kamil Python backend as child processes
- Auto-restarts services on crash (5s delay)
- System tray with context menu
- Ctrl+Space hotkey to toggle visibility
- Self-signed HTTPS support
- Window state persistence

**Service Flow:**
1. llama-server starts (waits ~60s for model load)
2. Kamil backend starts (60s after llama-server ready)
3. Web UI loads from https://localhost:3000
4. Services auto-restart if they crash

### 2. **Kamil Backend** (`interfaces/web/app.py`)

**Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serve web UI (index.html) |
| `/projects` | GET | List indexed projects |
| `/select_project` | POST | Load project for search |
| `/chat` | POST | Simple chat (no actions) |
| `/chat_actions` | POST | Chat with action parsing (SSE stream) |
| `/chat_agentic` | POST | Agentic search + response |
| `/council` | POST | Multi-agent deliberation |
| `/execute_action` | POST | Execute approved action |
| `/reset` | POST | Clear conversation history |
| `/synthesize` | POST | Text → WAV audio (Piper) |
| `/voice_chat` | POST | Text → Response + audio |
| `/transcribe` | POST | Audio → Text (Whisper) |

**Key Improvements:**
- Synchronous initialization (no Python 3.14 threading issues)
- Streaming SSE responses with pending action queues
- ChromaDB singleton client (shared across components)
- Intent classification (self/code/general) to prevent context leakage
- Voice pipeline: mic recording → Whisper transcription → Kamil response → Piper TTS → audio playback

### 3. **System Prompt** (`prompts/system.md`)

Externalized personality configuration:
- Identity: Persistent, offline, local-only AI companion
- Memory: References injected past conversations
- Personality: Direct, honest, peer-like (no filler)
- Scope: Assists with everything (code, life decisions, learning)
- Response style: Concise, context-aware

Loaded dynamically via `core/config.py` → no code restarts needed to tune behavior.

### 4. **Voice Pipeline**

**Capture:**
- Web Speech API → MediaRecorder for local audio recording
- Tap mic button to start/stop recording

**Transcription:**
- MediaRecorder → WebM blob → `/transcribe` endpoint
- Whisper STT (local model, no Google servers)
- Result injected into input field

**Response:**
- Kamil generates text response
- `/voice_chat` synthesizes audio using Piper
- Base64-encoded WAV returned to browser
- Auto-plays via `<audio>` element

### 5. **Context Management**

**Intent Classification** (`core/orchestrator.py._classify_intent()`):
- "self" — Memory/capability questions → skip code context
- "code" — Codebase questions → inject semantic search results
- "general" — Everything else → use history only
- "executor" — Shell commands → route to executor

**Smart Injection** (`_build_messages()`):
- Code context only injected for "code" intent
- System hints added for "self" intent to avoid code tangents
- Prevents wasted tokens and model confusion

**Memory Reuse:**
- LongTermMemory optionally reuses Embedder singleton
- Avoids loading SentenceTransformer twice
- Shared ChromaDB client via `get_chroma_client()`

### 6. **Reliability & Recovery**

**Indexed Project Cache** (`context/indexer.py`):
- Persists to `C:\kamil\data\indexed_projects.json`
- Skip re-indexing if already valid
- Self-healing: validates cache against ChromaDB
- Auto-removes from cache on corruption

**Database Integrity**:
- `is_indexed()` runs test query on collection
- Auto-detects corruption (empty or stale collections)
- Forces re-index on startup if needed
- No silent failures

**Process Resilience** (Electron app):
- Services monitored and auto-restarted
- Tray menu includes "Restart Services"
- Graceful shutdown on app quit

## Usage

### Installation

```bash
# Backend
cd C:\kamil
pip install -r requirements.txt
python run.py

# Desktop app
cd C:\kamil-app
npm install
npm start
```

### Configuration

**Backend:**
- System prompt: Edit `C:\kamil\prompts\system.md`
- Config: `C:\kamil\core\config.py` (ENV variables override)

**Desktop App:**
- Edit `src/main.js` for paths and URLs
- Or create `.env` file with settings (if implemented)

### Hotkeys

| Hotkey | Action |
|--------|--------|
| Ctrl+Space | Toggle window visibility |
| Enter | Send message |
| Shift+Enter | Newline in input |
| /council | Multi-agent deliberation |

### Voice Usage

1. **Speak to Kamil:**
   - Click 🎤 button to start recording
   - Speak clearly
   - Click 🎤 again to stop
   - Whisper transcribes → Kamil responds → Audio plays

2. **Tune Voice:**
   - Voice model: `C:\kamil\voices\en_US-amy-medium.onnx` (Piper)
   - Whisper model: Loaded on first use from HuggingFace

## File Structure

```
C:\kamil\
├── core/
│   ├── config.py                 (Loads prompts/system.md)
│   ├── orchestrator.py           (Intent classification)
│   ├── council.py
│   └── ...
├── context/
│   ├── embedder.py               (ChromaDB singleton)
│   ├── indexer.py                (Cache validation, self-healing)
│   └── ...
├── memory/
│   ├── long_term.py              (Reuses embedder)
│   ├── store.py
│   └── ...
├── interfaces/
│   └── web/
│       ├── app.py                (FastAPI, voice endpoints)
│       └── index.html            (MediaRecorder mic, SSE chat)
├── prompts/
│   └── system.md                 (External config)
├── data/
│   ├── indexed_projects.json     (Cache)
│   └── chroma/                   (ChromaDB persistent storage)
├── models/
│   └── qwen3.5-27b-claude-distill-q4.gguf
├── voices/
│   └── en_US-amy-medium.onnx
└── run.py

C:\kamil-app\
├── src/
│   ├── main.js                   (Electron main process)
│   └── preload.js                (IPC bridge)
├── package.json
├── forge.config.js
├── README.md
├── .gitignore
└── .env.example
```

## Development Notes

### Recent Fixes

1. **Python 3.14 Compatibility** — Removed daemon threads, sync init
2. **ChromaDB Conflicts** — Singleton client shared across components
3. **Cache Corruption** — Self-healing validation with test queries
4. **Context Leakage** — Intent classification prevents irrelevant injection
5. **Voice Transcription** — Whisper-based instead of Web Speech API

### Known Limitations

- Electron app hardcodes KAMIL_URL (could read from .env in future)
- Voice model paths hardcoded (refactor to config)
- No persistent settings in Electron app yet
- Model auto-download not implemented (manual placement required)

### Future Enhancements

- [ ] Tool/action schema validation
- [ ] Formalized agentic loops with retry logic
- [ ] Plugin system with /slash commands
- [ ] Persistent memory compression scheduler
- [ ] Web UI for managing indexed projects
- [ ] Electron settings window (paths, hotkeys, voice model)
- [ ] GPU monitoring in tray icon
- [ ] Local LLM inference telemetry

## Summary

Kamil is now a fully functional local AI desktop system with:
- ✅ Persistent memory via ChromaDB (singleton pattern)
- ✅ Intent-aware context injection (no irrelevant code)
- ✅ Voice I/O (Whisper STT + Piper TTS)
- ✅ External system prompt (tune personality without code)
- ✅ Electron desktop app (manages services, tray, hotkeys)
- ✅ Self-healing database integrity
- ✅ Streaming action approval workflow
- ✅ Multi-agent council for deliberation

Ready for production use on personal hardware, entirely offline.
