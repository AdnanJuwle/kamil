import sys
import os
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from core.orchestrator import Orchestrator
from context.indexer import Indexer
from actions.dispatcher import execute_action
from contextlib import asynccontextmanager
import json

# Initialize everything in main process before uvicorn starts
print("Initializing Kamil...")
kamil = Orchestrator()
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
def generate_sse(user_message: str):
    try:
        for chunk in kamil.chat_stream(user_message):
            data = json.dumps({"chunk": chunk})
            yield f"data: {data}\n\n"
        yield "data: [DONE]\n\n"
    except RuntimeError as e:
        error = json.dumps({"error": str(e)})
        yield f"data: {error}\n\n"