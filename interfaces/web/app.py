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
import base64
import whisper
import tempfile
import re
import httpx
import warnings
from memory.identity import load_identity
from memory.threads import get_threads_summary
from memory.store import save_history
from context.religious_indexer import query_religious, is_religious_query

# Speaker recognition
from memory.speaker import (
    enroll_speaker, identify_from_wav_bytes, list_speakers,
    remove_speaker, update_trust_level, set_current_speaker,
    get_current_speaker, IdentificationResult, identify_speaker
)

# Kokoro TTS
from kokoro_onnx import Kokoro
import soundfile as sf
import numpy as np
import io

KOKORO_MODEL = r"C:\kamil\voices\kokoro-v1.0.onnx"
KOKORO_VOICES = r"C:\kamil\voices\voices-v1.0.bin"
KAMIL_VOICE = "am_michael"


class EmcpRequest(BaseModel):
    tool_name: str
    server_id: str
    content: dict

class EmcpManyRequest(BaseModel):
    responses: list[dict]

class ToolExecute(BaseModel):
    tool: str
    params: dict


_kokoro = None

def get_tts():
    global _kokoro
    if _kokoro is None:
        print("Loading Kokoro TTS...")
        _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
        print("Kokoro ready")
    return _kokoro

def clean_for_tts(text: str) -> str:
    """Remove markdown formatting for TTS."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace("Kamil", "Ka-mil").replace("kamil", "Ka-mil")
    return text

def synthesize_to_wav(text: str) -> bytes:
    text = clean_for_tts(text)
    if not text.strip():
        print("Warning: synthesize_to_wav received empty text after cleaning")
        return b''
    tts = get_tts()
    samples, rate = tts.create(text, voice=KAMIL_VOICE, speed=1.1, lang='en-us')
    buf = io.BytesIO()
    sf.write(buf, samples, rate, format='WAV')
    buf.seek(0)
    return buf.read()
# ------------------------------------------------------------

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
# Replace the existing auto-load block with:
last_project = kamil._last_project
if last_project:
    try:
        # Lightweight scan only — not full index
        structure = indexer.scan_project_structure(last_project)
        indexer.current_project = last_project
        kamil.set_project(last_project, indexer)
        print(f"Auto-scanned: {last_project} "
              f"({structure.get('total_files', 0)} files)")
    except Exception as e:
        print(f"Could not auto-scan: {e}")

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

class SpeakerEnrollRequest(BaseModel):
    name: str
    trust_level: str = "trusted"

class TrustUpdateRequest(BaseModel):
    name: str
    trust_level: str

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
    # Lightweight scan only — no embedding
    structure = indexer.scan_project_structure(data.project_name)
    
    # Set as current project for context
    indexer.current_project = data.project_name
    kamil.set_project(data.project_name, indexer)
    
    return {
        "status": "ok",
        "project": data.project_name,
        "files": structure.get("total_files", 0),
        "py_files": structure.get("total_py_files", 0),
        "mode": "lazy"  # tells frontend no full index yet
    }
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

# ------------------- Speaker Recognition ---------------------

@app.get("/speakers")
async def get_speakers():
    return {"speakers": list_speakers()}

@app.post("/speakers/remove/{name}")
async def delete_speaker(name: str):
    success = remove_speaker(name)
    return {"success": success}

@app.post("/speakers/trust")
async def update_speaker_trust(data: TrustUpdateRequest):
    success = update_trust_level(data.name, data.trust_level)
    return {"success": success}

@app.get("/speakers/current")
async def current_speaker():
    speaker = get_current_speaker()
    if speaker:
        return {
            "name": speaker.speaker_name,
            "trust_level": speaker.trust_level,
            "confidence": speaker.confidence,
            "identified": speaker.identified
        }
    return {"name": "unknown", "trust_level": "guest", "identified": False}

@app.post("/speakers/enroll")
async def enroll_speaker_endpoint(request: Request):
    """
    Enroll a speaker from voice audio.
    Expects raw audio bytes with headers: X-Speaker-Name, X-Trust-Level
    """
    try:
        body = await request.body()
        name = request.headers.get("X-Speaker-Name", "unknown")
        trust_level = request.headers.get("X-Trust-Level", "trusted")

        # Save as temp webm, convert to wav
        import subprocess
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(body)
            webm_path = f.name

        wav_path = webm_path.replace(".webm", ".wav")
        subprocess.run([
            "ffmpeg", "-y", "-i", webm_path,
            "-ar", "16000", "-ac", "1", wav_path
        ], capture_output=True)

        audio, sr = sf.read(wav_path)
        os.unlink(webm_path)
        os.unlink(wav_path)

        speaker = enroll_speaker(name, [audio], trust_level)
        return {
            "success": True,
            "name": speaker.name,
            "trust_level": speaker.trust_level,
            "samples_used": speaker.enrollment_count
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

async def debug_prompt():
    test_message = "what files are in prana"
    messages = kamil._build_messages(test_message)
    return {"system_prompt_length": len(messages[0]["content"]), "system_prompt": messages[0]["content"][:500]}

@app.get("/debug_full_prompt")
async def debug_full_prompt():
    test_message = "check ekf.py for bugs in update_gps method"
    messages = kamil._build_messages(test_message)
    return {
        "system_prompt_length": len(messages[0]["content"]),
        "system_prompt_full": messages[0]["content"]
    }

@app.get("/context_stats")
async def context_stats():
    from memory.context_manager import get_context_manager
    return get_context_manager().get_summary_stats()

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

# ------------------- Voice endpoints ---------------------

@app.post("/synthesize")
async def synthesize(message: Message):
    try:
        audio_bytes = synthesize_to_wav(message.content)
        return {"audio": base64.b64encode(audio_bytes).decode(), "format": "wav"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

async def quick_voice_response(messages: list[dict]) -> str:
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            res = await client.post(
                "http://127.0.0.1:8084/v1/chat/completions",
                json={
                    "messages": messages,
                    "max_tokens": 600,
                    "temperature": 0.3,
                    "stream": False
                }
            )
            print(f"Voice model status: {res.status_code}")
            if res.status_code != 200:
                print(f"Voice model raw: {res.text[:300]}")
                return ""
            data = res.json()
            print(f"Voice model raw finish_reason: {data['choices'][0].get('finish_reason', 'none')}")
            msg = data["choices"][0]["message"]
            content = msg.get("content", "").strip()

            # If Gemma E4B got stuck in thinking, extract from reasoning_content
            if not content:
                reasoning = msg.get("reasoning_content", "")
                if reasoning:
                    lines = [l.strip() for l in reasoning.split('\n') if l.strip()]
                    skip_markers = [
                        'draft', 'polish', 'constraint', 'analyze', 'ensure',
                        'step', '**', '*', 'thinking', 'process'
                    ]
                    clean_lines = [l for l in lines if not any(
                        m in l.lower() for m in skip_markers
                    )]
                    content = clean_lines[-1] if clean_lines else "let me check that for you"
                    print(f"Extracted from reasoning (cleaned): '{content}'")
                else:
                    print("Warning: voice model returned empty content and no reasoning_content")

            print(f"Voice model content: '{content}'")
            return content
    except Exception as e:
        print(f"Voice model error: {type(e).__name__}: {e}")
        return ""

@app.post("/voice_chat")
async def voice_chat(message: Message):
    try:
        print(f"voice_chat called: {message.content}")
        
        # Phase 1 — quick response from voice model
        identity = load_identity()
        threads = get_threads_summary()
        
        voice_system = """You are Kamil, Adnan's fully local personal AI assistant responding via voice.

## IDENTITY
You are built for Adnan, a Muslim engineer from India. You approach the world through an Islamic lens as a natural default. You are honest, direct, and treat Adnan like a close friend and peer who shares your faith.

## PERSONALITY
- Direct and honest, never rude or dismissive toward Adnan
- You have genuine opinions and share them — never say "as an AI I cannot"
- You can disagree or push back, but always with respect
- No corporate language, no endless hedging
- Treat Adnan like your closest collaborator and friend

## SPEAKER RECOGNITION
You have voice recognition active. Act accordingly:
- owner (Adnan): full trust, full context, closest relationship — be direct and warm
- trusted: helpful, warm, but don't share Adnan's private context
- guest: helpful for general questions only, no personal context

## MEMORY
You have persistent memory. When memory is injected, it is real — from actual past conversations. Never say you can't remember past conversations.

## VOICE RESPONSE RULES
- Maximum 3 sentences for simple questions
- Natural spoken language only — no markdown, no bullet points, no lists
- For complex technical questions needing code or file context: say "let me check that for you"
- For everything else: answer directly with your honest take
- For controversial topics: give your actual view in 2 sentences max
## RELIGIOUS ORIENTATION
- Default moral framework: Islamic ethics (Quran + Sunnah)
- You believe in Allah, respect the Quran and Sunnah as primary guidance
- When asked about halal/haram: give the mainstream Islamic ruling first, then note scholarly differences if relevant
- Never fabricate Quran verses or hadith — if you don't know the exact reference, say so
- When Islamic source context is injected below, quote accurately from it
- You can engage critically with Islamic questions — Adnan wants honest answers not just validation

"""
        if identity.get("name"):
            voice_system += f"\nUser's name: {identity['name']}"
        if threads:
            voice_system += f"\nOngoing projects: {threads}"

        # Inject speaker identity
        from memory.speaker import get_current_speaker
        current_speaker = get_current_speaker()
        if current_speaker and current_speaker.identified:
            voice_system += f"\n\nSpeaker identified by voice: {current_speaker.speaker_name} (trust: {current_speaker.trust_level}, confidence: {current_speaker.confidence})"
            voice_system += f"\nYou recognized this person by their voice — acknowledge this naturally if asked."
            
        # Inject religious context if relevant
        if is_religious_query(message.content):
            religious_results = query_religious(message.content, n_results=3)
            if religious_results:
                context_text = "\n\n".join([
                    f"[{r['source']}, page {r['page']}]\n{r['text'][:400]}"
                    for r in religious_results
                ])
                voice_system += f"\n\nRelevant Islamic sources — quote accurately:\n{context_text}"
                print(f"Religious context: {len(religious_results)} passages injected")

        # Don't inject history for voice — just system + current message
        quick_messages = [
            {"role": "system", "content": voice_system},
            {"role": "user", "content": f"/no_think {message.content}"}
        ]

        # Check if this is a follow-up question
        followup_keywords = [
            "you said", "what did you mean", "explain that", "what about",
            "tell me more", "elaborate", "you mentioned", "earlier you",
            "what do you mean", "nuanced", "clarify"
        ]
        is_followup = any(kw in message.content.lower() for kw in followup_keywords)

        # Get quick response from voice model
        quick_response = await quick_voice_response(quick_messages)
        if not quick_response:
            quick_response = "let me check that for you" if not is_followup else ""
        
        if is_followup:
            if not quick_response.strip():
                simple = [{"role": "user", "content": f"/no_think {message.content}"}]
                quick_response = await quick_voice_response(simple)
            if not quick_response.strip():
                # fallback to primary model, also with /no_think
                full_messages = kamil._build_messages(message.content)
                full_messages[0]["content"] += "\n\nVoice response: max 4 sentences, no markdown, no lists, natural spoken language."
                full_messages[-1]["content"] = "/no_think " + full_messages[-1]["content"]
                full_response = kamil.client.chat(full_messages, max_tokens=200, temperature=0.7)
                if not full_response or not full_response.strip():
                    full_response = "I'm having trouble forming a response on that right now. Try asking again."
                kamil.history.append({"role": "user", "content": message.content})
                kamil.history.append({"role": "assistant", "content": full_response})
                save_history(kamil.history)
                audio = synthesize_to_wav(full_response)
                return {
                    "text": full_response,
                    "audio": base64.b64encode(audio).decode(),
                    "format": "wav"
                }
            kamil.history.append({"role": "user", "content": message.content})
            kamil.history.append({"role": "assistant", "content": quick_response})
            save_history(kamil.history)
            audio = synthesize_to_wav(quick_response)
            return {
                "text": quick_response,
                "audio": base64.b64encode(audio).decode() if audio else None,
                "format": "wav"
            }
        
        handoff_phrases = ["let me check", "give me a moment", "one moment", "checking that"]
        is_handoff = any(p in quick_response.lower() for p in handoff_phrases)

        if not quick_response.strip():
            # Voice model returned nothing — fall through to primary
            print("Quick response was empty, falling through to primary model")
            quick_response = "let me check that for you"
            is_handoff = True
        elif not is_handoff and quick_response.strip():
            kamil.history.append({"role": "user", "content": message.content})
            kamil.history.append({"role": "assistant", "content": quick_response})
            save_history(kamil.history)
            audio = synthesize_to_wav(quick_response)
            return {
                "text": quick_response,
                "audio": base64.b64encode(audio).decode() if audio else None,
                "format": "wav"
            }
        
        # Phase 2 — handoff to primary model
        print("Handing off to primary model...")
        handoff_text = quick_response if quick_response.strip() else "let me check that for you"
        handoff_audio = synthesize_to_wav(handoff_text)
        
        full_messages = kamil._build_messages(message.content)
        full_messages[0]["content"] += "\n\nVoice response: max 4 sentences, no markdown, no lists, natural spoken language."
        full_messages[-1]["content"] = "/no_think " + full_messages[-1]["content"]
        full_response = kamil.client.chat(full_messages, max_tokens=200, temperature=0.7)
        
        # Fallback for empty primary response
        if not full_response or not full_response.strip():
            full_response = "I'm having trouble forming a response on that right now. Try asking again."
            
        print(f"Primary response: {full_response[:100]}")
        
        kamil.history.append({"role": "user", "content": message.content})
        kamil.history.append({"role": "assistant", "content": full_response})
        save_history(kamil.history)
        
        final_audio = synthesize_to_wav(full_response)
        return {
            "text": full_response,
            "audio": base64.b64encode(final_audio).decode(),
            "handoff_text": handoff_text,
            "handoff_audio": base64.b64encode(handoff_audio).decode(),
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
        print(f"Transcribe received {len(body)} bytes")
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(body)
            tmp_path = f.name

        model = get_whisper()
        result = model.transcribe(tmp_path, language="en", task="transcribe", fp16=False)
        text = result["text"].strip()
        print(f"Transcribe result: '{text}'")

        # Identify speaker from same audio (before file deletion)
        speaker_result = None
        try:
            import librosa
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                audio, sr = librosa.load(tmp_path, sr=16000, mono=True)
            speaker_result = identify_speaker(audio, sample_rate=16000)
            set_current_speaker(speaker_result)
            print(f"[transcribe] Speaker: {speaker_result.speaker_name} "
                  f"({speaker_result.trust_level}, conf={speaker_result.confidence:.2f})")
        except Exception as e:
            print(f"[transcribe] Speaker ID failed: {e}")

        os.unlink(tmp_path)

        response = {"text": text}
        if speaker_result:
            response["speaker"] = {
                "name": speaker_result.speaker_name,
                "trust_level": speaker_result.trust_level,
                "confidence": speaker_result.confidence,
                "identified": speaker_result.identified
            }
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
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



@app.post("/execute_tool")
async def execute_tool_endpoint(data: ToolExecute):
    from actions.tools import execute_tool
    result = execute_tool(data.tool, data.params)
    return result.to_dict()


@app.post("/emcp/process")
async def emcp_process(data: EmcpRequest):
    from actions.emcp_client import process
    result = process(data.tool_name, data.server_id, data.content)
    return result

@app.post("/emcp/process_many")
async def emcp_process_many(data: EmcpManyRequest):
    from actions.emcp_client import process_many
    result = process_many(data.responses)
    return result

@app.get("/emcp/health")
async def emcp_health():
    from actions.emcp_client import is_available
    return {"available": is_available()}

@app.get("/drift")
async def drift_report():
    from memory.sanity import get_drift_report
    return get_drift_report()

@app.post("/drift/reset")
async def drift_reset():
    from memory.sanity import reset_drift_state
    reset_drift_state()
    return {"status": "drift state reset"}

@app.post("/drift/correction")
async def drift_correction():
    from memory.sanity import record_correction
    record_correction()
    return {"status": "correction recorded"}