from models.client import LlamaClient
from memory.threads import add_thread, load_threads, update_thread
import json
import re

client = LlamaClient()

def summarize_session(history: list[dict]) -> str:
    if len(history) < 4:
        return ""
    
    conversation = "\n".join([
        f"{m['role'].upper()}: {m['content'][:300]}"
        for m in history[-20:]
    ])
    
    messages = [
        {"role": "system", "content": """Extract a brief summary from this conversation. Output JSON with:
{
  "key_topics": ["topic1", "topic2"],
  "decisions_made": ["decision1"],
  "open_questions": ["question1"],
  "one_line_summary": "brief summary"
}
Output only valid JSON, nothing else."""},
        {"role": "user", "content": conversation}
    ]
    
    try:
        response = client.chat(messages, temperature=0.1, max_tokens=300)
        clean = re.sub(r'```json|```', '', response).strip()
        return json.loads(clean)
    except:
        return {}

def update_threads_from_session(history: list[dict]):
    summary = summarize_session(history)
    if not summary or not isinstance(summary, dict):
        return
    
    one_liner = summary.get("one_line_summary", "")
    topics = summary.get("key_topics", [])
    
    if not one_liner:
        return
    
    threads = load_threads()
    # Check if this matches an existing thread
    existing_titles = [t["title"] for t in threads]
    
    for topic in topics:
        matched = False
        for t in threads:
            if any(word in t["title"].lower() for word in topic.lower().split()):
                update_thread(t["id"], one_liner)
                matched = True
                break
        if not matched and topic:
            add_thread(topic, one_liner, tags=topics)
            break