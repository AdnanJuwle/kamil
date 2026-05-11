# Kamil Complete File Structure

```
C:\kamil\
├── core/
│   ├── config.py                    [UPDATED] Loads prompts/system.md dynamically
│   ├── orchestrator.py              [COMPLETE] Intent classification, message building
│   ├── council.py                   [STABLE] Multi-agent deliberation
│   └── __init__.py
│
├── context/
│   ├── embedder.py                  [COMPLETE] ChromaDB singleton (get_chroma_client)
│   ├── indexer.py                   [COMPLETE] Self-healing cache validation
│   └── __init__.py
│
├── memory/
│   ├── long_term.py                 [COMPLETE] Reuses embedder singleton
│   ├── store.py
│   └── __init__.py
│
├── interfaces/
│   └── web/
│       ├── app.py                   [UPDATED] Added /transcribe, /synthesize, /voice_chat
│       ├── index.html               [COMPLETE] MediaRecorder voice UI, SSE chat
│       └── ...
│
├── actions/
│   ├── dispatcher.py                [STABLE] Execute and parse actions
│   ├── filesystem.py                [STABLE] File operations
│   └── shell.py                     [STABLE] Shell commands
│
├── prompts/
│   └── system.md                    [NEW] External personality config
│
├── data/
│   ├── indexed_projects.json        [AUTO] Project cache (self-healing)
│   ├── chroma/                      [AUTO] ChromaDB persistent storage
│   └── ...
│
├── models/
│   └── qwen3.5-27b-claude-distill-q4.gguf
│
├── voices/
│   └── en_US-amy-medium.onnx        (Piper TTS model)
│
├── venv313/                         (Virtual environment)
│   └── Scripts/python.exe
│
├── run.py                           [ENTRY] Start backend
├── requirements.txt
├── README.md                        [COMPLETE] Backend docs
└── .gitignore

C:\kamil-app\
├── src/
│   ├── main.js                      [NEW] Electron main process (services, tray, hotkeys)
│   └── preload.js                   [NEW] Secure IPC bridge to renderer
│
├── package.json                     [NEW] Dependencies, scripts
├── forge.config.js                  [NEW] Squirrel installer config
├── README.md                        [NEW] Electron app docs
├── .gitignore                       [NEW]
├── .env.example                     [NEW] Configuration template
└── out/                             (Generated on build)
    └── make/
        └── squirrel.windows/        (Installer .exe)

C:\ (root)
├── SYSTEM_BUILD.md                  [NEW] Complete architecture & components
├── QUICK_START.md                   [NEW] Setup & usage guide
├── IMPLEMENTATION_CHECKLIST.md      [NEW] Feature status & testing
└── FILE_STRUCTURE.md                [NEW] This file
```

## File Purposes & Dependencies

### Backend Core

**config.py**
- Purpose: Configuration, environment variables, system prompt loading
- Depends: os, prompts/system.md
- Used by: orchestrator.py, all modules

**orchestrator.py**
- Purpose: Main AI orchestration, message building, intent classification
- Depends: context.indexer, memory.long_term, memory.store, actions.dispatcher
- Methods: chat(), chat_with_actions(), chat_agentic(), council_chat(), _classify_intent(), _build_messages()

**indexer.py**
- Purpose: Project indexing, caching, semantic search
- Depends: context.embedder, ChromaDB, SentenceTransformer
- Key: is_indexed() with self-healing, _remove_from_indexed_cache()

**embedder.py**
- Purpose: Embeddings, ChromaDB access
- Depends: chromadb, sentence-transformers
- Key: get_chroma_client() singleton, prevents duplicate clients

**long_term.py**
- Purpose: Persistent memory with semantic search
- Depends: chromadb, embedder (optional reuse)
- Key: Accepts optional embedder to avoid loading model twice

### Web Interface

**app.py**
- Purpose: FastAPI backend, endpoints, streaming
- Depends: orchestrator, indexer, piper, whisper, tempfile
- Endpoints: /, /projects, /select_project, /chat, /chat_actions, /council, /execute_action, /synthesize, /voice_chat, /transcribe
- Key: Synchronous init (no threads), SSE streaming, voice endpoints

**index.html**
- Purpose: Web UI for chat, voice, project selection
- Features: MediaRecorder for voice, SSE parser, action approval panel
- CSS: Dark theme with accent colors, responsive layout
- JS: send(), toggleMic(), sendVoiceMessage(), etc.

### External Config

**system.md**
- Purpose: AI personality and behavior guidelines
- Format: Markdown sections (IDENTITY, MEMORY, PERSONALITY, SCOPE, etc.)
- Loaded by: core/config.py at startup
- Edit without restart: Not true — must restart backend to reload

### Desktop App

**main.js**
- Purpose: Electron main process, service management
- Dependencies: electron, child_process, https
- Features: Spawn llama-server + Kamil backend, tray icon, hotkeys, process monitoring
- Config: Hardcoded paths (KAMIL_DIR, MODEL, PYTHON, LLAMA_SERVER)

**preload.js**
- Purpose: Secure bridge between main and renderer
- Exposes: kamilApp.getStatus(), kamilApp.platform

**package.json**
- Purpose: Node dependencies, Electron Forge config, build scripts
- Key: electron-forge for Squirrel installer generation

**forge.config.js**
- Purpose: Electron Forge configuration for packaging
- Output: Creates .exe installer in out/make/

### Data Files

**indexed_projects.json**
- Location: C:\kamil\data\
- Format: JSON array of project names and index status
- Auto-generated: Yes (on first project index)
- Self-heals: Yes (invalid entries auto-removed)
- Validated: On startup via is_indexed()

**chroma/**
- Location: C:\kamil\data\chroma\
- Format: ChromaDB persistent storage (binary)
- Auto-generated: Yes (first collection creation)
- Shared access: Via get_chroma_client() singleton

## Dependency Chain

```
app.py
  ├── orchestrator.py
  │   ├── indexer.py
  │   │   ├── embedder.py (singleton)
  │   │   │   └── ChromaDB
  │   │   └── config.py
  │   ├── long_term.py
  │   │   ├── embedder.py (reused)
  │   │   └── ChromaDB
  │   ├── actions.dispatcher
  │   └── config.py
  ├── piper (Piper TTS)
  ├── whisper (OpenAI Whisper)
  └── config.py

index.html
  ├── /chat_actions → app.py
  ├── /voice_chat → app.py
  ├── /transcribe → app.py
  ├── /execute_action → app.py
  └── MediaRecorder API (browser)

main.js (Electron)
  ├── llama-server process
  ├── python run.py (Kamil backend)
  └── window → index.html → app.py
```

## Configuration Paths

| Setting | File | Format | Override |
|---------|------|--------|----------|
| System prompt | prompts/system.md | Markdown | Edit file, restart backend |
| API URL | core/config.py | Python | Env var KAMIL_SERVER_URL |
| Timeout | core/config.py | Python | Env var KAMIL_TIMEOUT |
| Temperature | core/config.py | Python | Env var KAMIL_TEMPERATURE |
| Max tokens | core/config.py | Python | Env var KAMIL_MAX_TOKENS |
| Electron URL | kamil-app/src/main.js | JavaScript | Edit main.js |
| Electron paths | kamil-app/src/main.js | JavaScript | Edit main.js |

## Build & Deploy

### Development
```bash
cd C:\kamil
python run.py

# In separate terminal
cd C:\kamil-app
npm start
```

### Packaging
```bash
cd C:\kamil-app
npm run build

# Creates:
# out/make/squirrel.windows/Kamil-1.0.0 Setup.exe
```

### Distribution
- Build creates self-signed installer
- Run installer to create Start Menu shortcuts
- App auto-updates services on startup
- Logs to console (if running from terminal)

## Version Info

- Backend: Python 3.13 (3.14+ breaks threading → process exit)
- Frontend: HTML5, CSS3, ES6 JavaScript
- Desktop: Electron 33+, Node 18+
- Framework: FastAPI, Pydantic, ChromaDB
- ML Models: Qwen 3.5 (LLM), Whisper (STT), Piper (TTS), MiniLM (embeddings)

## Status

✅ **Production Ready** — All components implemented, tested, documented
