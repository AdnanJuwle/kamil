import sys
import os
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
sys.path.insert(0, r'C:\kamil')
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from core.orchestrator import Orchestrator
from core import activity
from context.indexer import Indexer
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

class AgentResume(BaseModel):
    run_id: str
    approvals: list[bool] = []   # one per pending tool, in order; True = approve


_kokoro = None

def _enable_cuda_tts() -> bool:
    """Put the nvidia cuDNN/cuBLAS/CUDA-runtime wheel DLLs on the search path and
    select the CUDA provider for onnxruntime (Kokoro). add_dll_directory alone does
    NOT reach ORT's dependent-DLL resolution — PATH does — so we set both. Returns
    True if CUDA is usable. Falls back to CPU cleanly otherwise."""
    try:
        import onnxruntime as _ort, glob
        if "CUDAExecutionProvider" not in _ort.get_available_providers():
            return False
        nvidia_root = os.path.abspath(os.path.join(os.path.dirname(_ort.__file__), "..", "nvidia"))
        bins = [os.path.abspath(p) for p in glob.glob(os.path.join(nvidia_root, "*", "bin")) if os.path.isdir(p)]
        if not bins:
            return False
        os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
        for b in bins:
            try:
                os.add_dll_directory(b)
            except Exception:
                pass
        os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
        return True
    except Exception as _e:
        print(f"Kokoro TTS: CUDA setup failed, CPU ({_e})")
        return False

def get_tts():
    global _kokoro
    if _kokoro is None:
        # Kokoro on CPU runs ~1.4x real-time (the voice bottleneck). Run it on the
        # GPU when available — modest (~1.3x) since the ONNX graph has many Memcpy
        # nodes, but it keeps synthesis ahead of streamed playback.
        print("Kokoro TTS:", "CUDAExecutionProvider (GPU)" if _enable_cuda_tts() else "CPU")
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
kamil.enable_context(indexer)
# On startup — only scan project names, don't load any project
available_projects = indexer.list_projects()
print(f"Projects available (lazy): {', '.join(available_projects)}")
# No auto-load, no auto-scan

# Warm up Kokoro TTS in the background (load onto GPU + compile kernels) so the
# first voice reply isn't a cold ~15s stall.
import threading as _thr
def _warm_tts():
    try:
        synthesize_to_wav("Ready.")
        print("Kokoro warmup done")
    except Exception as _e:
        print(f"Kokoro warmup failed: {_e}")
_thr.Thread(target=_warm_tts, daemon=True).start()

# ── Heartbeat: Kamil's proactive presence ─────────────────────────
import threading
from datetime import datetime as _dt

HEARTBEAT_SECONDS = int(os.getenv("KAMIL_HEARTBEAT_SECONDS", "60"))
HEARTBEAT_ON = os.getenv("KAMIL_HEARTBEAT", "1") == "1"
_proactive_queue: list = []
_proactive_lock = threading.Lock()
_heartbeat_stop = threading.Event()

def _heartbeat_tick():
    from core import presence
    from core import reminders as reminders_mod
    from memory.thread_state import load_thread_state

    now = _dt.now()
    active_thread = None
    try:
        ts = load_thread_state()
        if ts and getattr(ts, "is_active", False) and ts.message_count:
            try:
                lu = _dt.fromisoformat(ts.last_updated)
            except Exception:
                lu = None
            active_thread = {
                "id": ts.thread_id, "type": ts.thread_type,
                "focus": ts.current_focus or ts.root_issue,
                "last_updated": lu, "message_count": ts.message_count,
            }
    except Exception:
        pass

    intent = presence.evaluate(
        now,
        active_thread=active_thread,
        due_reminders=reminders_mod.due_reminders(now),
        last_proactive=presence.get_last_proactive(),
        delivered_triggers=presence.get_delivered_triggers(),
    )
    if not intent:
        return

    text = kamil.proactive_message(intent.reason, intent.text)
    with _proactive_lock:
        _proactive_queue.append({"text": text, "kind": intent.kind, "ts": now.isoformat()})
    if intent.kind == "reminder" and intent.payload.get("reminder_id"):
        reminders_mod.mark_fired(intent.payload["reminder_id"])
    presence.record_proactive(intent, now)
    print(f"[heartbeat] proactive ({intent.kind}): {text[:70]}")

def _heartbeat_loop():
    while not _heartbeat_stop.is_set():
        try:
            _heartbeat_tick()
        except Exception as e:
            print(f"[heartbeat] tick error: {e}")
        _heartbeat_stop.wait(HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(app):
    indexer.start_watching()
    if HEARTBEAT_ON:
        threading.Thread(target=_heartbeat_loop, daemon=True).start()
        print(f"[heartbeat] presence loop started ({HEARTBEAT_SECONDS}s)")
    yield
    _heartbeat_stop.set()
    indexer.stop_watching()

app = FastAPI(lifespan=lifespan)

class Message(BaseModel):
    content: str

class ProjectSelect(BaseModel):
    project_name: str

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
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/presence")
async def presence_poll():
    """The UI polls this; returns (and clears) any proactive messages Kamil queued."""
    with _proactive_lock:
        msgs = _proactive_queue[:]
        _proactive_queue.clear()
    if msgs:
        for m in msgs:
            kamil.history.append({"role": "assistant", "content": m["text"]})
        save_history(kamil.history)
    return {"messages": msgs}

CHAT_LABEL = os.getenv("KAMIL_CHAT_LABEL", "lexi-8b")
HEAVY_LABEL = os.getenv("KAMIL_HEAVY_LABEL", "coder-14b")

@app.get("/status")
async def status():
    """Live tier + schedule state for the top bar. Reflects what's actually
    loaded: the chat model (8080) stays resident; the heavy model (8086) is
    on-demand and idle-unloads."""
    from core import heavy_tier, schedule, config
    try:
        chat_code = httpx.get(f"{config.LLAMA_SERVER_URL}/health", timeout=1).status_code
    except Exception:
        chat_code = 0
    chat_state = "live" if chat_code == 200 else ("loading" if chat_code == 503 else "off")
    now = _dt.now()
    return {
        "tiers": [
            {"id": "chat", "label": "chat", "model": CHAT_LABEL, "state": chat_state},
            {"id": "heavy", "label": "heavy", "model": heavy_tier.label(), "state": heavy_tier.state()},
        ],
        "block": schedule.current_block(now),
        "quiet": schedule.is_quiet_hours(now),
        "project": getattr(indexer, "current_project", None),
    }

def _memory_body(content: str) -> str:
    """The editable notes body — content minus the header/comment lines."""
    keep = [l for l in content.splitlines()
            if not (l.strip().startswith("#") or l.strip().startswith("<!--"))]
    return "\n".join(keep).strip()

@app.get("/memory")
async def memory_view():
    """Kamil's self-authored memory.md — the notes he keeps about his world."""
    from core import self_memory
    content = self_memory.load_memory()
    return {"content": content, "body": _memory_body(content)}

class MemoryBody(BaseModel):
    body: str

class MemoryForget(BaseModel):
    query: str

@app.post("/memory")
async def memory_save(data: MemoryBody):
    """Hand-edit: replace the whole notes body (header preserved)."""
    from core import self_memory
    return self_memory.set_memory(data.body)

@app.post("/memory/forget")
async def memory_forget(data: MemoryForget):
    """Prune a single note by its text."""
    from core import self_memory
    return self_memory.forget(data.query)

def _memory_notes() -> list[str]:
    from core import self_memory
    return [l.strip()[2:].strip() for l in self_memory.load_memory().splitlines()
            if l.strip().startswith("- ") and len(l.strip()) > 4]

@app.get("/holding")
async def holding_view():
    """What Kamil is holding in mind right now: the active thread's focus and a
    note from his own memory. Feeds the presence column's whispers."""
    import random
    whispers = []
    try:
        from memory.thread_state import load_thread_state
        ts = load_thread_state()
        if ts and getattr(ts, "is_active", False) and ts.message_count:
            focus = (ts.current_focus or ts.root_issue or "").strip()
            if focus:
                whispers.append({"text": focus, "src": "this thread"})
    except Exception:
        pass
    notes = _memory_notes()
    if notes:
        whispers.append({"text": random.choice(notes), "src": "my memory"})
    return {"whispers": whispers}

@app.get("/sealed")
async def sealed_view():
    """Sealed succession directive — status only. Never the word, never the will."""
    from core import succession
    heir_enrolled = any(
        s.get("name") == succession.HEIR_NAME and s.get("trust_level") == "owner"
        for s in list_speakers()
    )
    return {
        "exists": succession.seal_exists(),
        "activated": succession.is_activated(),
        "owner": succession.CREATOR_NAME,
        "heir": succession.HEIR_NAME,
        "heir_voice_enrolled": heir_enrolled,
    }

@app.get("/owner")
async def owner_view():
    """A glance at who Kamil belongs to: the owner, enrolled voices, one note."""
    from core import succession
    about = next((n for n in _memory_notes() if "adnan" in n.lower()), "")
    return {"owner": succession.CREATOR_NAME, "voices": list_speakers(), "about": about}

@app.get("/skills")
async def skills_view():
    """The skill library: domains (folders) + the docs Kamil has learned."""
    from context.domain_indexer import DOMAIN_BOOKS_DIR
    domains = []
    if os.path.isdir(DOMAIN_BOOKS_DIR):
        for d in sorted(os.listdir(DOMAIN_BOOKS_DIR)):
            dp = os.path.join(DOMAIN_BOOKS_DIR, d)
            if os.path.isdir(dp):
                files = [f for f in sorted(os.listdir(dp)) if os.path.isfile(os.path.join(dp, f))]
                domains.append({"domain": d, "files": files})
    total = 0
    try:
        from context.embedder import get_chroma_client
        total = get_chroma_client().get_collection("kamil_domain").count()
    except Exception:
        pass
    return {"domains": domains, "total_chunks": total}

@app.get("/web")
async def web_search_view(q: str = ""):
    """A web search from the UI. Only the query leaves the machine."""
    from actions.web import search_web
    if not q.strip():
        return {"results": []}
    return {"results": search_web(q, 6), "query": q}

@app.get("/activity")
async def activity_poll():
    """Background work Kamil is doing right now (indexing, scans, model loads,
    speaker checks) + what just finished. The presence panel polls this."""
    snap = activity.snapshot()
    snap["watching"] = bool(getattr(indexer, "current_project", None))
    snap["project"] = getattr(indexer, "current_project", None)
    return snap

@app.get("/actions")
async def actions_view(n: int = 80):
    """Persistent history of everything Kamil has DONE — tool calls + background
    jobs, newest first. The Work tab shows this."""
    from core import action_log
    return {"actions": action_log.recent(n)}

@app.post("/actions/clear")
async def actions_clear():
    from core import action_log
    action_log.clear()
    return {"status": "cleared"}

@app.get("/vision/status")
async def vision_status():
    """Is the vision tier usable, and why not if not. The app/device polls this."""
    from core import vision_tier
    return {"available": vision_tier.available(), "state": vision_tier.state(),
            "label": vision_tier.label(), "reason": vision_tier.missing_reason()}

@app.get("/vision/current")
async def vision_current():
    """What Kamil is looking at right now (live thumbnail + question/answer). The
    presence panel polls this to show the frame being analysed."""
    from core import vision_live
    return vision_live.current()

@app.post("/vision")
async def vision(image: UploadFile = File(...),
                 question: str = Form("What is in this image? Describe it concisely and read any visible text.")):
    """Look at ONE uploaded image with the local vision model — the endpoint a camera
    or wearable pushes frames to. The frame is processed in memory and NOT stored."""
    from core import vision_tier
    if not vision_tier.available():
        return {"ok": False, "error": vision_tier.missing_reason()}
    data = await image.read()
    import mimetypes
    mime = image.content_type or mimetypes.guess_type(image.filename or "")[0] or "image/jpeg"
    from actions.tools import _vision_answer
    answer, err = _vision_answer(data, question, image.filename or "upload", mime)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "answer": answer}

@app.post("/learn")
async def learn(file: UploadFile = File(...), domain: str = Form("general")):
    """Upload a doc -> Kamil ingests it as a skill (immediately queryable)."""
    import shutil
    from context.domain_indexer import learn_document, DOMAIN_BOOKS_DIR
    safe_domain = "".join(c for c in domain if c.isalnum() or c in "_-").lower() or "general"
    dest_dir = os.path.join(DOMAIN_BOOKS_DIR, safe_domain)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(file.filename))
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with activity.task("indexing skill", f"{os.path.basename(file.filename)} · {safe_domain}"):
            result = learn_document(dest, domain=safe_domain)
        return result
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "chunks": 0}

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
    # Kamil opens projects himself now (auto-switch on mention, or the
    # open_project tool); this endpoint stays as a manual override / API hook and
    # routes through the same single open_project path.
    ok = kamil.open_project(data.project_name)
    return {"status": "ok" if ok else "error", "project": indexer.current_project}
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

@app.post("/chat_agent")
async def chat_agent(message: Message):
    """Agentic tool-use loop — streams structured events (tool calls + results)."""
    return StreamingResponse(
        generate_agent_sse(message.content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.post("/agent_resume")
async def agent_resume(data: AgentResume):
    """Resume a paused agent run after the user approves/denies its pending tools."""
    run = _agent_runs.pop(data.run_id, None)
    if not run:
        async def _gone():
            yield f"data: {json.dumps({'type': 'error', 'error': 'unknown or expired run_id'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_gone(), media_type="text/event-stream")

    from actions.tools import execute_tool
    executed = []
    for i, call in enumerate(run["pending"]):
        approved = data.approvals[i] if i < len(data.approvals) else False
        if approved:
            res = execute_tool(call["tool"], call["params"]).to_dict()
        else:
            res = {"success": False, "error": "denied by user", "tool": call["tool"]}
        executed.append((call, res))

    return StreamingResponse(
        _forward_agent_events(
            kamil.resume_agent(run["transcript"], executed, run["user_message"]),
            run["user_message"],
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.get("/time")
async def current_time():
    from datetime import datetime
    now = datetime.now()
    return {
        "time": now.strftime("%I:%M %p"),
        "date": now.strftime("%B %d, %Y"),
        "day": now.strftime("%A"),
        "weekday": now.weekday()
    }

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

import time as _time
_voice_up_cache = {"ok": None, "ts": 0.0}

def _voice_model_up() -> bool:
    """Is the separate fast voice model (8084) actually running? Cached ~15s so
    we don't probe every request. When it's down, /voice_chat skips the two-stage
    quick-pass entirely and answers straight from the primary model — no bogus
    'let me check that for you' filler, no wasted failed connection."""
    now = _time.time()
    if _voice_up_cache["ok"] is not None and now - _voice_up_cache["ts"] < 15:
        return _voice_up_cache["ok"]
    try:
        ok = httpx.get("http://127.0.0.1:8084/health", timeout=0.5).status_code == 200
    except Exception:
        ok = False
    _voice_up_cache.update(ok=ok, ts=now)
    return ok

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

VOICE_SYSTEM_BASE = """You are Kamil, Adnan's fully local personal AI assistant responding via voice.

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

def _build_voice_messages(content: str) -> list[dict]:
    """The lean voice prompt (identity + speaker + threads + religious context),
    with the handoff line stripped since there's no second stage. Small + stable
    prefix, so llama caches it and replies are quick."""
    voice_system = VOICE_SYSTEM_BASE
    identity = load_identity()
    threads = get_threads_summary()
    if identity.get("name"):
        voice_system += f"\nUser's name: {identity['name']}"
    if threads:
        voice_system += f"\nOngoing projects: {threads}"
    from memory.speaker import get_current_speaker
    cs = get_current_speaker()
    if cs and cs.identified:
        voice_system += (f"\n\nSpeaker identified by voice: {cs.speaker_name} "
                         f"(trust: {cs.trust_level}, confidence: {cs.confidence})"
                         f"\nYou recognized this person by their voice — acknowledge this naturally if asked.")
    if is_religious_query(content):
        rr = query_religious(content, n_results=3)
        if rr:
            ctx = "\n\n".join(f"[{r['source']}, page {r['page']}]\n{r['text'][:400]}" for r in rr)
            voice_system += f"\n\nRelevant Islamic sources — quote accurately:\n{ctx}"
    voice_system = voice_system.replace(
        '- For complex technical questions needing code or file context: say "let me check that for you"',
        '- Answer everything directly and concisely in a spoken tone — never defer or say you will "check"')
    voice_system = voice_system.replace(
        '- Maximum 3 sentences for simple questions',
        '- Maximum 2-3 short sentences for EVERY reply — even big/complex questions')
    voice_system += ("\n\nSTRICT SPOKEN LIMIT: Your reply is read aloud. Use at most 2-3 short "
                     "sentences and ALWAYS finish your thought within them — never write paragraphs, "
                     "lists, or trail off. If a topic is large, give the essence in a sentence or two "
                     "and offer to go deeper if asked.")
    # No /no_think prefix: that's a Qwen/Gemma reasoning token; the Llama chat model
    # echoes it as literal text ("No_think, ...").
    return [{"role": "system", "content": voice_system},
            {"role": "user", "content": content}]

@app.post("/voice_agent_stream")
async def voice_agent_stream(message: Message):
    """Voice call in AGENT mode: run the tool-using agent on the spoken request
    (read files, web search, run code), speak short status while it works, then
    speak a concise result. This is the default call mode — most calls are real
    requests, not chit-chat."""
    import asyncio
    async def gen():
        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue()
        def worker():
            try:
                for ev in kamil.chat_agent(message.content, voice=True):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})
            loop.call_soon_threadsafe(q.put_nowait, None)
        loop.run_in_executor(None, worker)

        final_text = ""
        spoke_filler = False
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    break
                t = ev.get("type")
                if t in ("tool_call", "status") and not spoke_filler:
                    spoke_filler = True
                    yield f"data: {json.dumps({'type': 'status', 'text': 'working on it'})}\n\n"
                elif t in ("text", "done", "max_iterations") and ev.get("text"):
                    final_text = ev["text"]
                elif t == "awaiting_confirmation":
                    final_text = "That one needs your approval on screen before I can do it."
                elif t == "error":
                    final_text = "Sorry, I ran into an error on that."
            if not final_text:
                final_text = "I couldn't complete that."
            print(f"Voice (agent): {final_text[:100]}")
            yield f"data: {json.dumps({'type': 'text', 'text': final_text})}\n\n"
            spoken = clean_for_tts(final_text)
            if spoken.strip():
                tts = get_tts(); seq = 0
                async for samples, sr in tts.create_stream(spoken, voice=KAMIL_VOICE, speed=1.1, lang="en-us"):
                    buf = io.BytesIO(); sf.write(buf, samples, sr, format="WAV"); buf.seek(0)
                    yield f"data: {json.dumps({'type': 'audio', 'seq': seq, 'audio': base64.b64encode(buf.read()).decode()})}\n\n"
                    seq += 1
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            import traceback; traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.post("/voice_chat_stream")
async def voice_chat_stream(message: Message):
    """Voice reply as an SSE stream: the text first, then Kokoro audio chunk by
    chunk (create_stream) so playback starts on the first sentence instead of
    waiting for the whole reply to synthesize. This is what makes voice feel fast."""
    async def gen():
        try:
            resp = kamil.client.chat(_build_voice_messages(message.content), max_tokens=160, temperature=0.7)
            text = (resp or "").strip() or "I'm having trouble forming a response on that right now. Try asking again."
            print(f"Voice (stream): {text[:100]}")
            kamil.history.append({"role": "user", "content": message.content})
            kamil.history.append({"role": "assistant", "content": text})
            save_history(kamil.history)
            yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
            spoken = clean_for_tts(text)
            if spoken.strip():
                tts = get_tts()
                # Kokoro's native create_stream batches by phoneme count and trims
                # silence between batches for seamless concatenation — so chunks
                # play GAPLESS. (Manual per-sentence create() left a synth-lag gap
                # between sentences.) Short replies come as one chunk; longer ones
                # stream in contiguous batches.
                seq = 0
                async for samples, sr in tts.create_stream(spoken, voice=KAMIL_VOICE, speed=1.1, lang="en-us"):
                    buf = io.BytesIO(); sf.write(buf, samples, sr, format="WAV"); buf.seek(0)
                    yield f"data: {json.dumps({'type': 'audio', 'seq': seq, 'audio': base64.b64encode(buf.read()).decode()})}\n\n"
                    seq += 1
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            import traceback; traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.post("/voice_chat")
async def voice_chat(message: Message):
    try:
        print(f"voice_chat called: {message.content}")

        # Phase 1 — quick response from voice model
        identity = load_identity()
        threads = get_threads_summary()

        voice_system = VOICE_SYSTEM_BASE
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

        # No separate voice model running -> answer straight from the primary model
        # using the LEAN voice prompt (already built above), NOT the heavy
        # _build_messages (full memory dump + domain RAG over ~29k chunks + code
        # context). The lean prompt is ~500 tokens and its prefix stays stable, so
        # llama caches it and each reply is a quick eval + short generation instead
        # of re-processing thousands of tokens every turn. Strip the handoff line
        # since there's no second stage to hand off to.
        if not _voice_model_up():
            lean_system = voice_system.replace(
                '- For complex technical questions needing code or file context: say "let me check that for you"',
                '- Answer everything directly and concisely in a spoken tone — never defer or say you will "check"')
            lean_messages = [
                {"role": "system", "content": lean_system},
                {"role": "user", "content": f"/no_think {message.content}"},
            ]
            full_response = kamil.client.chat(lean_messages, max_tokens=160, temperature=0.7)
            if not full_response or not full_response.strip():
                full_response = "I'm having trouble forming a response on that right now. Try asking again."
            print(f"Primary response (no voice model): {full_response[:100]}")
            kamil.history.append({"role": "user", "content": message.content})
            kamil.history.append({"role": "assistant", "content": full_response})
            save_history(kamil.history)
            final_audio = synthesize_to_wav(full_response)
            return {"text": full_response, "audio": base64.b64encode(final_audio).decode(), "format": "wav"}

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

# Phrases Whisper commonly hallucinates from silence / non-speech audio.
WHISPER_GHOSTS = {
    "music", "thank you", "thanks", "thank you.", "thanks for watching",
    "thank you for watching", "please subscribe", "you", "bye", "so", "okay",
    "uh", "um", "hmm", ".", "[music]", "[blank_audio]", "subtitles by the amara.org community",
}

def _looks_like_silence(text: str, result: dict, audio) -> bool:
    """True when the transcript is almost certainly a Whisper hallucination on
    silence — so we don't fire a voice reply at nothing."""
    t = (text or "").lower().strip(" .!?,\"'")
    if not t:
        return True
    energy = 0.0
    try:
        import numpy as np
        if audio is not None and len(audio):
            energy = float(np.sqrt(np.mean(np.square(audio))))
    except Exception:
        pass
    segs = result.get("segments") or []
    nsp = (sum(s.get("no_speech_prob", 0.0) for s in segs) / len(segs)) if segs else 1.0
    if energy and energy < 0.003:          # essentially silent input
        return True
    if t in WHISPER_GHOSTS and nsp > 0.5:  # a known ghost + Whisper's own doubt
        return True
    if nsp > 0.9:                           # Whisper itself says "no speech"
        return True
    return False

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

        # Load the clip once (for the silence guard + speaker id)
        audio = None
        try:
            import librosa
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                audio, _sr = librosa.load(tmp_path, sr=16000, mono=True)
        except Exception as e:
            print(f"[transcribe] audio load failed: {e}")

        # Drop Whisper hallucinations on silence (e.g. an accidental empty tap
        # that comes back as "Music") so Kamil doesn't answer to nothing.
        if _looks_like_silence(text, result, audio):
            print(f"[transcribe] dropped as silence/hallucination: '{text}'")
            text = ""

        # Identify speaker from the same audio
        speaker_result = None
        if audio is not None:
            try:
                with activity.task("verifying speaker"):
                    speaker_result = identify_speaker(audio, sample_rate=16000)
                set_current_speaker(speaker_result)
                print(f"[transcribe] Speaker: {speaker_result.speaker_name} "
                      f"({speaker_result.trust_level}, conf={speaker_result.confidence:.2f})")
                # Sealed succession directive — the only place it can activate.
                # Both factors (the heir's verified voice + the spoken word) come
                # from this single audio clip, so they cannot be assembled apart.
                if text:
                    try:
                        from core.succession import scan_spoken
                        scan_spoken(text, speaker_result)
                    except Exception as _se:
                        print(f"[transcribe] succession scan skipped: {_se}")
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
        print(f"[agentic_sse] Error: {e}")
        error = json.dumps({"error": str(e)})
        yield f"data: {error}\n\n"
        yield "data: [DONE]\n\n"

import uuid
# Paused agent runs awaiting user confirmation, keyed by run_id. The transcript
# is held server-side so it never round-trips through the browser.
_agent_runs: dict = {}

def _forward_agent_events(event_iter, user_message: str = ""):
    """Stream agent events as SSE, stashing any pause transcript server-side and
    sending the client only a run_id + the pending tool calls."""
    try:
        for event in event_iter:
            if event.get("type") == "awaiting_confirmation":
                run_id = uuid.uuid4().hex[:12]
                _agent_runs[run_id] = {
                    "transcript": event.get("transcript", []),
                    "pending": event.get("pending", []),
                    "user_message": user_message,
                }
                yield f"data: {json.dumps({'type': 'awaiting_confirmation', 'run_id': run_id, 'pending': event.get('pending', [])})}\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        print(f"[agent_sse] Error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

def generate_agent_sse(user_message: str):
    return _forward_agent_events(kamil.chat_agent(user_message), user_message)

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

@app.get("/domain_stats")
async def domain_stats():
    from context.domain_indexer import query_domain
    from context.embedder import get_chroma_client
    client = get_chroma_client()
    try:
        col = client.get_collection("kamil_domain")
        return {"total_chunks": col.count(), "status": "ready"}
    except Exception:
        return {"total_chunks": 0, "status": "not indexed"}