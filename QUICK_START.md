# Kamil Quick Start Guide

## 1. Backend Setup

### Prerequisites
- Python 3.13 (NOT 3.14 — threading issues)
- llama-server compiled
- Model file downloaded

### Installation

```bash
cd C:\kamil

# Create virtual environment
python -m venv venv313
.\venv313\Scripts\activate

# Install dependencies
pip install fastapi uvicorn pydantic chromadb sentence-transformers piper-tts whisper torch
```

### Configuration

Edit `core/config.py` if needed:
```python
LLAMA_SERVER_URL = "http://127.0.0.1:8080"
EXECUTOR_SERVER_URL = "http://127.0.0.1:8083"
DEFAULT_TEMPERATURE = 0.7
```

Edit personality: `prompts/system.md`

### Starting Backend

```bash
cd C:\kamil
python run.py
```

Backend runs on `http://localhost:3000` (web UI) and talks to llama-server on `:8080`.

## 2. Electron App Setup

### Prerequisites
- Node.js 18+ installed
- Kamil backend running
- HTTPS cert setup (self-signed OK)

### Installation

```bash
cd C:\kamil-app
npm install
```

### Configuration

Edit `src/main.js` for your paths:
```javascript
const KAMIL_URL = 'https://localhost:3000'  // Change IP if needed
const KAMIL_DIR = 'C:\\kamil'
const PYTHON = 'C:\\kamil\\venv313\\Scripts\\python.exe'
const LLAMA_SERVER = 'C:\\llama.cpp\\build\\bin\\llama-server.exe'
const MODEL = 'C:\\kamil\\models\\qwen3.5-27b-claude-distill-q4.gguf'
```

### Starting App

```bash
npm start
```

This will:
1. Spawn llama-server (waits ~60s for model load)
2. After 60s → spawn Kamil Python backend
3. Load web UI in Electron window

## 3. Using Kamil

### Text Chat
- Type in input box
- Press Enter to send
- Chat appears with streaming responses

### Voice Chat
1. Click 🎤 button (starts recording)
2. Speak clearly
3. Click 🎤 again to stop
4. Whisper transcribes audio
5. Kamil responds
6. Audio plays automatically

### Multi-Agent Deliberation
- Type `/council` + your question
- Multiple agents discuss the topic
- Final synthesis provided

### Project Indexing
1. Select project from dropdown
2. Click "load"
3. Chat will now include code context from that project

## 4. Hotkeys

| Shortcut | Action |
|----------|--------|
| Ctrl+Space | Show/hide window |
| Enter | Send message |
| Shift+Enter | Newline |

## 5. System Prompt Tuning

Edit `C:\kamil\prompts\system.md`:

```markdown
# Kamil System Prompt

You are Kamil, a fully local AI system...

## PERSONALITY
- Direct. One or two sentences when that is enough.
- Honest. If something is wrong...
```

Restart backend to apply changes.

## 6. Common Issues

### "Connection refused"
- Check if llama-server is running: `curl http://localhost:8080/health`
- Check if Kamil backend is running: `curl http://localhost:3000/`

### Voice not working
- Ensure Whisper model installed: `pip install openai-whisper`
- Check if Piper voice model exists: `C:\kamil\voices\en_US-amy-medium.onnx`
- Allow browser microphone access when prompted

### High memory usage
- Reduce context size in main.js: `-c 4096` instead of `-c 8192`
- Reduce GPU layers: `-ngl 50` instead of `-ngl 99`

### HTTPS certificate error
- Electron app ignores self-signed certs (set in main.js)
- If browser complains: use http instead of https (less secure)

## 7. Project Structure

**Backend** (`C:\kamil\`):
- `core/` — Orchestration, config, intent classification
- `context/` — Indexing, embeddings, search
- `memory/` — Long-term memory with ChromaDB
- `interfaces/web/` — FastAPI server, web UI
- `prompts/` — External system prompt

**Desktop App** (`C:\kamil-app\`):
- `src/main.js` — Electron main process (services, tray)
- `src/preload.js` — Secure IPC bridge
- `package.json` — Dependencies

**Data** (`C:\kamil\data\`):
- `indexed_projects.json` — Cache of indexed projects
- `chroma/` — ChromaDB persistent storage

## 8. Upgrading / Resetting

### Clear Conversation History
- Click "clear" button in UI (top right)

### Re-index Project
1. Delete from `indexed_projects.json` cache
2. Reload project in UI → forces re-index

### Reset Everything
```bash
# Stop backend
# Delete all data
rm -r C:\kamil\data\*
# Restart backend
python run.py
```

## 9. Monitoring

### Check Processes
```bash
# Check if services running
tasklist | findstr "llama-server"
tasklist | findstr "python"
```

### Check Logs
- Electron app: DevTools (`Ctrl+Shift+I`)
- Backend: Console output (if running directly)
- Indexing: `C:\kamil\data\indexed_projects.json` (cache state)

## 10. Next Steps

- [ ] Tune system prompt in `prompts/system.md`
- [ ] Test with a code project (select project → ask questions)
- [ ] Test voice (click mic → speak → listen to response)
- [ ] Join Kamil Discord/GitHub for community tips
- [ ] Consider custom hotkeys (edit main.js)
- [ ] Set up model auto-download (future feature)

---

**Questions?** Check SYSTEM_BUILD.md for architecture details.
