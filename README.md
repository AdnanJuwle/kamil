# Kamil

**Local, offline AI assistant with deep code understanding and action execution.**

Kamil is a privacy-first development assistant that runs entirely on your machine. It combines multiple LLMs (Qwen, Mistral, Llama 3.1) for balanced analysis, indexes your project code for semantic search, and can execute actions with human approval.

## Features

- **Offline & Private**: Runs 100% locally. No data leaves your machine.
- **Code Understanding**: Automatically indexes your project using embeddings for semantic search and context retrieval.
- **Multi-LLM Council**: Three LLMs analyze every question from different angles:
  - **Analyst** (Qwen): Deep technical analysis
  - **Devil's Advocate** (Mistral): Critical evaluation
  - **Synthesizer** (Llama 3.1): Balanced conclusion
- **Action Execution**: Execute file operations, code changes, and more with built-in approval workflow.
- **Multiple Interfaces**:
  - VS Code extension for seamless integration
  - Web UI for standalone use
- **Smart Caching**: Indexed projects are cached and automatically re-indexed when code changes.
- **Crash-Resistant**: Self-healing database with automatic corruption detection and recovery.

## Architecture

```
┌─────────────────────┐
│   VS Code Ext       │  ← JavaScript/TypeScript webview
│   or Web UI         │
└──────────┬──────────┘
           │ WebSocket/HTTP
           ▼
┌─────────────────────┐
│   FastAPI Server    │  ← Python backend (interfaces/web/app.py)
└─────────┬───────────┘
          │
    ┌─────┴──────┬────────────┬──────────────┐
    ▼            ▼            ▼              ▼
┌────────┐  ┌─────────┐  ┌──────────┐  ┌──────────┐
│ Indexer│  │Orchestr.│  │  Embedder│  │ Dispatcher
└────────┘  └─────────┘  └──────────┘  └──────────┘
    │
    └──► ChromaDB (local vector store)
    └──► File Watcher (auto re-index on changes)
```

## Installation

### Requirements
- Python 3.13+ (not 3.14 due to uvicorn compatibility issues)
- 8GB+ RAM (for running LLMs locally)
- ~20GB disk space (for models and indices)

### Setup

1. **Clone the repository**
```bash
git clone https://github.com/AdnanJuwle/kamil.git
cd kamil
```

2. **Create virtual environment**
```bash
python -m venv venv
venv\Scripts\activate  # Windows
# or: source venv/bin/activate  # Linux/Mac
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Configure paths** (in `context/indexer.py` and `core/config.py`)
- Set `PROJECTS_DIR` to your code repositories
- Set `DATA_DIR` for cache/embeddings storage
- Update `INDEXED_CACHE_PATH` if needed

5. **Start the server**
```bash
cd interfaces/web
python -m uvicorn app:app --host 127.0.0.1 --port 3000 --loop asyncio
```

The server will initialize on first run (30-60 seconds while indexing), then be ready at `http://127.0.0.1:3000`

### VS Code Extension

1. Copy `kamil/src/` to VS Code extensions folder
2. Or run `npm install && npm run compile` in `kamil/`
3. Reload VS Code and activate the Kamil extension

## Usage

### Web UI
- Navigate to `http://127.0.0.1:3000`
- Select a project to enable code context
- Type questions or use COUNCIL for multi-LLM analysis
- Use ACTION mode for executable responses

### VS Code Extension
- Open the Kamil sidebar (Ctrl+Shift+K)
- Select current code or ask questions
- SEND routes through action parser (model decides text vs actions)
- COUNCIL spawns three-way LLM debate

## Project Structure

```
kamil/
├── core/                 # Orchestrator, config, history
├── context/              # Indexer, embedder, file watcher
├── actions/              # Action dispatcher and execution
├── interfaces/
│   ├── web/             # FastAPI backend + HTML UI
│   └── vscode/          # VS Code extension source
├── models/              # LLM model definitions
└── README.md
```

## Key Design Decisions

### Synchronous Startup
- All heavy initialization happens in the main process before uvicorn starts
- Avoids Python 3.14 + threading + async conflicts
- First startup takes 30-60s (one-time), subsequent startups are instant

### Self-Healing Cache
- JSON cache tracks indexed projects
- On startup, validates cache against actual ChromaDB state
- If collection is empty or corrupted, automatically re-indexes
- No manual database cleanup ever needed

### ChromaDB with Vector Storage
- Uses sentence-transformers (all-MiniLM-L6-v2) for embeddings
- Local SQLite backend with crash-resistant settings
- Cosine similarity for semantic search
- Automatic file change detection and re-indexing

### Multi-LLM Council
- Runs three different models in parallel for balanced analysis
- Each model has distinct personality (analyst vs skeptic vs synthesizer)
- Results combined for more thoughtful responses

### Action Execution Framework
- Model generates action JSON
- User reviews and approves/rejects each action
- File operations logged and recoverable
- Type-checked action dispatch

## Troubleshooting

### Server crashes on startup
- Check Python version: `python --version` (should be 3.13.x)
- Check stdout for import errors (stderr merged to stdout)
- Verify paths in config files exist and are readable

### "Collection is empty" or "index not found"
- This is expected on first run
- Server will automatically index on first startup
- If it happens repeatedly, the auto-healing triggered
- Check `C:\kamil\data\indexed_projects.json` was cleared

### Database corruption
If ChromaDB gets into a bad state:
```bash
# Delete corrupted data
rmdir /s /q C:\kamil\data\chroma
mkdir C:\kamil\data\chroma

# Clear cache to force re-index
del C:\kamil\data\indexed_projects.json

# Restart server
```

Or just restart the server — the self-healing mechanism will detect corruption and handle it automatically.

## Performance

- **First index**: 30-60 seconds depending on project size
- **Subsequent loads**: <100ms (cached)
- **File re-indexing**: 1-5 seconds for modified files
- **Query response**: 5-30 seconds (LLM inference time)
- **Council response**: 15-90 seconds (three models in parallel)

## Architecture Notes

- **No external API calls**: Everything runs offline
- **No data collection**: Anonymized telemetry disabled
- **Model selection**: Qwen for analysis, Mistral for critique, Llama for synthesis
- **Thread safety**: Synchronous main process avoids GIL/threading issues
- **Database consistency**: ChromaDB with SQLite persistence + WAL crash recovery

## Future Improvements

- [ ] Local LLM fine-tuning on project code
- [ ] Incremental indexing with dependency tracking
- [ ] Multi-workspace support
- [ ] Custom action handlers
- [ ] Model quantization for faster inference

## License

MIT

## Contributing

Pull requests welcome. Please ensure:
- Code follows existing style
- No external API dependencies
- Offline-first design maintained
- Python 3.13+ compatibility

---

**Built for developers who value privacy and local control.**
