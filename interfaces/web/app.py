import sys
import os
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
sys.path.insert(0, r'C:\kamil')
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from core.orchestrator import Orchestrator
from context.indexer import Indexer
from actions.dispatcher import execute_action
from contextlib import asynccontextmanager
import json
from piper import PiperVoice
import wave
import io
import base64
import whisper
import tempfile


from kokoro_onnx import Kokoro
import soundfile as sf
import numpy as np
import io

KOKORO_MODEL = r"C:\kamil\voices\kokoro-v1.0.onnx"
KOKORO_VOICES = r"C:\kamil\voices\voices-v1.0.bin"
KAMIL_VOICE = "am_michael"

_kokoro = None

def get_tts():
    global _kokoro
    if _kokoro is None:
        print("Loading Kokoro TTS...")
        _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
        print("Kokoro ready")
    return _kokoro

def synthesize_to_wav(text: str) -> bytes:
    # Fix Kamil pronunciation
    text = text.replace("Kamil", "Ka-mil").replace("kamil", "Ka-mil")
    tts = get_tts()
    samples, rate = tts.create(text, voice=KAMIL_VOICE, speed=1.0, lang='en-us')
    buf = io.BytesIO()
    sf.write(buf, samples, rate, format='WAV')
    buf.seek(0)
    return buf.read()
# Whisper model (load once)
_whisper_model = None
def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Loading Whisper on {device}...")
        _whisper_model = whisper.load_model("small", device="cpu")
        print("Whisper ready")
    return _whisper_model

# Initialize everything in main process before uvicorn starts
print("Initializing Kamil...")
kamil = Orchestrator()
print("Loading Whisper...")
get_whisper()
print("Whisper ready")
indexer = Indexer()

# Auto-load last project synchronously before server starts
last_project = kamil._last_project
if last_project:
    try:
        projects = indexer.list_projects()
        if last_project in projects:
            if not indexer.is_indexed(last_project):
                print(f"Indexing {last_project}...")
                indexer.index_project(last_project)
            else:
                indexer.current_project = last_project
            kamil.set_project(last_project, indexer)
            print(f"Auto-loaded: {last_project}")
    except Exception as e:
        print(f"Could not auto-load: {e}")

@asynccontextmanager
async def lifespan(app):
    indexer.start_watching()
    yield
    indexer.stop_watching()

app = FastAPI(lifespan=lifespan)

class Message(BaseModel):
    content: str

class ProjectSelect(BaseModel):
    project_name: str

class ActionConfirm(BaseModel):
    action: dict

class IdentityUpdate(BaseModel):
    key: str
    value: object

@app.get("/")
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r") as f:
        return HTMLResponse(f.read())

@app.get("/projects")
async def list_projects():
    return {"projects": indexer.list_projects()}

@app.get("/current_project")
async def current_project():
    return {"project": indexer.current_project}

@app.get("/history")
async def get_history():
    return {"history": kamil.history, "project": indexer.current_project}

@app.post("/select_project")
async def select_project(data: ProjectSelect):
    already_indexed = indexer.is_indexed(data.project_name)
    if indexer.current_project == data.project_name or already_indexed:
        indexer.current_project = data.project_name
        kamil.set_project(data.project_name, indexer)
        return {"status": "ok", "project": data.project_name, "chunks": "cached"}
    def status(current, total, path):
        pass
    total = indexer.index_project(data.project_name, status_callback=status)
    kamil.set_project(data.project_name, indexer)
    return {"status": "ok", "project": data.project_name, "chunks": total}

@app.post("/chat")
async def chat(message: Message):
    return StreamingResponse(
        generate_sse(message.content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.post("/reset")
async def reset():
    kamil.reset()
    return {"status": "conversation reset"}

@app.post("/council")
async def council(message: Message):
    try:
        result = kamil.council_chat(message.content)
        return result
    except RuntimeError as e:
        return {"error": str(e)}

@app.post("/execute_action")
async def confirm_action(data: ActionConfirm):
    result = execute_action(data.action)
    return result

@app.post("/chat_actions")
async def chat_actions(message: Message):
    return StreamingResponse(
        generate_action_sse(message.content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.get("/debug")
async def debug():
    test_results = indexer.embedder.query("prana", "sensor interface", n_results=3) if indexer.current_project else []
    return {
        "current_project": indexer.current_project,
        "context_enabled": kamil.context_enabled,
        "test_query_results": len(test_results)
    }

@app.get("/debug_prompt")
async def debug_prompt():
    test_message = "what files are in prana"
    messages = kamil._build_messages(test_message)
    return {"system_prompt_length": len(messages[0]["content"]), "system_prompt": messages[0]["content"][:500]}

@app.post("/chat_agentic")
async def chat_agentic(message: Message):
    return StreamingResponse(
        generate_agentic_sse(message.content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.post("/update_identity")
async def update_identity_endpoint(data: IdentityUpdate):
    from memory.identity import update_identity
    result = update_identity(data.key, data.value)
    return {"status": "ok", "identity": result}

@app.get("/identity")
async def get_identity():
    from memory.identity import load_identity, is_new_user
    return {"identity": load_identity(), "new_user": is_new_user()}

# Voice synthesis (correct definitions)
VOICE_MODEL_PATH = r"C:\kamil\voices\en_US-amy-medium.onnx"
_voice = None

def get_voice():
    global _voice
    if _voice is None:
        _voice = PiperVoice.load(VOICE_MODEL_PATH)
    return _voice

def synthesize_to_wav(text: str) -> bytes:
    voice = get_voice()
    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(voice.config.sample_rate)
        voice.synthesize(text, wav_file)
    audio_buffer.seek(0)
    return audio_buffer.read()

@app.post("/synthesize")
async def synthesize(message: Message):
    try:
        audio_bytes = synthesize_to_wav(message.content)
        return {"audio": base64.b64encode(audio_bytes).decode(), "format": "wav"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

@app.post("/voice_chat")
async def voice_chat(message: Message):
    try:
        response_text = kamil.chat(message.content)
        audio_bytes = synthesize_to_wav(response_text)
        return {
            "text": response_text,
            "audio": base64.b64encode(audio_bytes).decode(),
            "format": "wav"
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "text": str(e), "audio": None}

@app.post("/transcribe")
async def transcribe(request: Request):
    try:
        body = await request.body()
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(body)
            tmp_path = f.name
        model = get_whisper()
        result = model.transcribe(tmp_path)
        os.unlink(tmp_path)
        return {"text": result["text"].strip()}
    except Exception as e:
        return {"error": str(e)}

# SSE generators
def generate_sse(user_message: str):
    try:
        for chunk in kamil.chat_stream(user_message):
            data = json.dumps({"chunk": chunk})
            yield f"data: {data}\n\n"
        yield "data: [DONE]\n\n"
    except RuntimeError as e:
        error = json.dumps({"error": str(e)})
        yield f"data: {error}\n\n"

def generate_agentic_sse(user_message: str):
    try:
        for chunk in kamil.chat_agentic(user_message):
            data = json.dumps({"chunk": chunk})
            yield f"data: {data}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        error = json.dumps({"error": str(e)})
        yield f"data: {error}\n\n"

def generate_action_sse(user_message: str):
    try:
        result = kamil.chat_with_actions(user_message)
        data = json.dumps({"chunk": result["response"]})
        yield f"data: {data}\n\n"
        if result.get("pending_confirmation"):
            conf_data = json.dumps({"pending_confirmation": result["pending_confirmation"]})
            yield f"data: {conf_data}\n\n"
        yield "data: [DONE]\n\n"
    except RuntimeError as e:
        error = json.dumps({"error": str(e)})
        yield f"data: {error}\n\n"