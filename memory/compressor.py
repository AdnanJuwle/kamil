from models.client import LlamaClient
from memory.long_term import LongTermMemory
import json
import re

client = LlamaClient()
long_term = LongTermMemory()

def compress_messages(messages: list[dict], current_project: str = "") -> str:
    if not messages:
        return ""
    
    conversation = "\n".join([
        f"{m['role'].upper()}: {m['content'][:400]}"
        for m in messages
    ])
    
    prompt_messages = [
        {"role": "system", "content": """Compress this conversation into a dense memory summary.

Include:
- Key facts learned about the user
- Decisions made
- Problems solved
- Work in progress
- Important context for future conversations

Output JSON:
{
  "summary": "dense paragraph preserving all important context",
  "topics": ["topic1", "topic2"],
  "project": "main project discussed or empty string"
}

Be specific and factual. Preserve technical details. Output only valid JSON."""},
        {"role": "user", "content": conversation}
    ]
    
    try:
        response = client.chat(prompt_messages, temperature=0.1, max_tokens=400)
        clean = re.sub(r'```json|```', '', response).strip()
        data = json.loads(clean)
        summary = data.get("summary", "")
        topics = data.get("topics", [])
        project = data.get("project", current_project)
        if summary:
            long_term.store(summary, topics, project)
        return summary
    except Exception as e:
        # Fallback — store raw summary if JSON parsing fails
        raw = f"Conversation about: {conversation[:200]}"
        long_term.store(raw, [], current_project)
        return raw

def should_compress(history: list[dict], threshold: int = 8) -> bool:
    return len(history) >= threshold