from models.client import LlamaClient
from memory.long_term import LongTermMemory
import json
import re

client = LlamaClient()
long_term = LongTermMemory()

from memory.sanity import validate_compression, record_interaction, is_learning_enabled

def compress_messages(messages: list[dict], current_project: str = "") -> str:
    if not messages:
        return ""

    # Check if learning is disabled due to drift
    if not is_learning_enabled():
        print("[compressor] Learning disabled due to drift — skipping compression")
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

        if not summary:
            print("[compressor] Empty summary from model — skipping storage")
            return ""

        # Sanity check before storing
        validation = validate_compression(summary, messages)
        print(f"[compressor] Memory validation: score={validation.score:.2f}, valid={validation.valid}, reason={validation.reason}")

        # Record for drift monitoring
        record_interaction(
            user_message=conversation[:200],
            response=summary,
            was_corrected=False,
            validation_result=validation
        )

        if validation.valid:
            long_term.store(summary, topics, project)
            print(f"[compressor] Memory stored: {summary[:80]}...")
            return summary
        else:
            print(f"[compressor] Memory REJECTED: {validation.reason}")
            print(f"[compressor] Failed checks: {validation.checks_failed}")
            # Don't store bad memory
            return ""

    except json.JSONDecodeError as e:
        print(f"[compressor] JSON parse error: {e} — skipping storage")
        return ""
    except Exception as e:
        print(f"[compressor] Compression error: {e}")
        return ""

def should_compress(history: list[dict], threshold: int = 8) -> bool:
    return len(history) >= threshold