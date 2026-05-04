# orchestrator.py
from models.client import LlamaClient
from core.config import (
    KAMIL_SYSTEM_PROMPT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS,
    LLAMA_SERVER_URL, EXECUTOR_SERVER_URL, VOICE_SERVER_URL
)
from actions.dispatcher import parse_actions, requires_confirmation, execute_action
from memory.store import save_history, load_history, clear_history, save_setting, load_setting
from memory.identity import load_identity, is_new_user, update_identity
from memory.threads import get_threads_summary
from memory.long_term import LongTermMemory
from memory.compressor import compress_messages, should_compress
from typing import Generator
import json
import re
from core.config import LLAMA_SERVER_URL, EXECUTOR_SERVER_URL, VOICE_SERVER_URL
from context.religious_indexer import query_religious, is_religious_query

VOICE_HANDOFF_PHRASES = [
    "let me check that for you",
    "give me a moment",
    "let me look into that",
    "one moment",
    "checking that now",
]

VOICE_SMALL_SYSTEM = """You are Kamil, a local AI assistant responding via voice. Keep all responses under 3 sentences.

If the question is simple (greetings, basic facts, personal questions, memory questions) — answer directly and completely.

If the question requires deep reasoning, code analysis, project-specific knowledge, or detailed technical explanation — respond with ONLY this exact phrase: "let me check that for you"

Never say anything else as a handoff. Either answer fully or say exactly "let me check that for you"."""


class Orchestrator:
    def __init__(self):
        self.client = LlamaClient(base_url=LLAMA_SERVER_URL)
        self.voice_client = LlamaClient(base_url=VOICE_SERVER_URL)
        self.executor_client = LlamaClient(base_url=EXECUTOR_SERVER_URL)
        self.history = load_history()
        self.system_prompt = KAMIL_SYSTEM_PROMPT
        self.indexer = None
        self.context_enabled = False
        self.council = None
        self._init_council()
        self.long_term = LongTermMemory()  # Will set embedder after indexer is set
        last_project = load_setting("last_project")
        self._last_project = last_project if last_project else None
        self.voice_client = LlamaClient(base_url=VOICE_SERVER_URL)

        self._new_user = is_new_user()

    def _init_council(self):
        try:
            from core.council import Council
            self.council = Council()
        except Exception:
            self.council = None

    def _maybe_compress(self):
        if should_compress(self.history):
            # Compress oldest 8 messages
            to_compress = self.history[:8]
            current_project = self.indexer.current_project if self.indexer else ""
            compress_messages(to_compress, current_project)
            # Keep only recent messages
            self.history = self.history[8:]
            save_history(self.history)

    def _executor_prompt(self, user_message: str) -> str:
        return f"""You are an executor. Perform the requested action directly using action tags.
    Available actions:
    <action type="write_file" path="C:\\full\\path\\file.py">content</action>
    <action type="read_file" path="C:\\full\\path\\file.py" />
    <action type="shell" command="command here" />
    <action type="list_directory" path="C:\\full\\path" />
    <action type="create_task" title="title" project="project">description</action>

    Rules:
    - Use real paths only, never placeholders
    - Default write location: C:\\kamil\\workspace\\
    - Active project: {self.indexer.current_project if self.indexer else 'none'}
    - Active project path: C:\\Users\\adnan\\Documents\\repos\\{self.indexer.current_project if self.indexer else ''}

    Task: {user_message}"""

    def run_executor(self, user_message: str) -> dict:
        prompt = self._executor_prompt(user_message)
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self.executor_client.chat(messages, temperature=0.1, max_tokens=500)
        except RuntimeError:
            # Executor not running — fall back to primary
            response = self.client.chat(
                [{"role": "system", "content": self.system_prompt},
                 {"role": "user", "content": user_message}],
                temperature=0.1, max_tokens=500
            )
    
        clean_text, actions = parse_actions(response)
        auto_executed = []
        pending_confirmation = []
    
        for action in actions:
            if requires_confirmation(action):
                pending_confirmation.append(action)
            else:
                result = execute_action(action)
                auto_executed.append({"action": action, "result": result})
                if result.get("success") and action["type"] == "read_file":
                    clean_text += f"\n\n[Read {action['path']} — {result['lines']} lines]"
    
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": clean_text or "Done."})
        save_history(self.history)
    
        return {
            "response": clean_text or "Done.",
            "auto_executed": auto_executed,
            "pending_confirmation": pending_confirmation
        }

    def _classify_intent(self, message: str) -> str:
        msg = message.lower()
        
        self_keywords = [
            "do you remember", "what do you know", "what have we discussed",
            "your memory", "can you recall", "what did we", "do you know me",
            "who am i", "what am i working on", "longest memory", "how much do you remember",
            "what do you remember", "tell me about yourself", "your capabilities"
        ]

        code_keywords = [
            "function", "class", "method", "file", "folder", "directory", "import",
            "bug", "error", "implement", "code", "algorithm", "variable", "module",
            "does it", "how does", "what does", "find", "check", "inspect", "review"
        ]

        # Only classify as code if project is loaded
        if self.context_enabled and self.indexer:
            for kw in code_keywords:
                if kw in msg:
                    return "code"

        return "general"

    def _build_messages(self, user_message: str) -> list[dict]:
        intent = self._classify_intent(user_message)
        system = self.system_prompt

        # Always inject identity and memory
        identity = load_identity()
        threads = get_threads_summary()
        memories = self.long_term.retrieve(user_message, n_results=3)

        memory_block = ""
        if identity.get("name"):
            memory_block += f"User's name: {identity['name']}\n"
        if identity.get("preferences"):
            prefs = ", ".join([f"{k}: {v}" for k, v in identity["preferences"].items()])
            memory_block += f"Preferences: {prefs}\n"
        if identity.get("context"):
            ctx = ", ".join([f"{k}: {v}" for k, v in identity["context"].items()])
            memory_block += f"About the user: {ctx}\n"
        if threads:
            memory_block += f"\nOngoing projects:\n{threads}"
        if memory_block:
            system += f"\n\nWhat you know about the user:\n{memory_block}"

        if memories:
            memory_text = "\n\n".join([
                f"[{m['timestamp'][:10]}] {m['text']}"
                for m in memories
            ])
            system += f"\n\nRelevant memories from past conversations:\n{memory_text}"

        if self._new_user and not self.history:
            system += "\n\nThis is a new user. Introduce yourself briefly and ask their name naturally."

        # Religious context for text chat
        if is_religious_query(user_message):
            religious_results = query_religious(user_message, n_results=3)
            if religious_results:
                context_text = "\n\n".join([
                    f"[{r['source']}, page {r['page']}]\n{r['text'][:400]}"
                    for r in religious_results
                ])
                system += f"\n\nRelevant Islamic sources — quote accurately from these, never fabricate verses:\n{context_text}"

        # Only inject code context for code intent
        context_prefix = ""
        if intent == "code" and self.context_enabled and self.indexer:
            results = self.indexer.query(user_message, n_results=6)
            if results:
                context_block = "\n\n".join([
                    f"[{r['metadata']['file_path'].split(chr(92))[-1]} | {r['metadata']['name']}]\n{r['text'][:800]}"
                    for r in results
                ])
                context_prefix = f"Retrieved code context:\n\n{context_block}\n\nUser query: "
        elif intent == "self":
            system += "\n\nThe user is asking about you or your memory. Answer from your knowledge about the user and your memory system. Do not reference code or project files."

        trimmed_history = self.history[-6:] if len(self.history) > 6 else self.history
        total_chars = len(system) + len(context_prefix) + sum(len(m["content"]) for m in trimmed_history)
        if total_chars > 20000:
            trimmed_history = self.history[-2:] if len(self.history) > 2 else self.history

        messages = [{"role": "system", "content": system}] + trimmed_history
        if context_prefix:
            messages.append({"role": "user", "content": context_prefix + user_message})
        else:
            messages.append({"role": "user", "content": user_message})
        if intent == "self":
            print(f"DEBUG self intent — memory block: {memory_block[:200]}")
            print(f"DEBUG memories retrieved: {len(memories)}")
        return messages

    def chat_stream(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        full_response = ""
        for chunk in self.client.chat_stream(messages, temperature=temperature, max_tokens=max_tokens):
            full_response += chunk
            yield chunk
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_response})
        save_history(self.history)

    def chat(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        response = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": response})
        save_history(self.history)
        return response

    def chat_with_actions(self, user_message: str) -> dict:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        full_response = self.client.chat(messages)
        clean_text, actions = parse_actions(full_response)
        auto_executed = []
        pending_confirmation = []
        for action in actions:
            if requires_confirmation(action):
                pending_confirmation.append(action)
            else:
                result = execute_action(action)
                auto_executed.append({"action": action, "result": result})
                if result.get("success") and action["type"] == "read_file":
                    clean_text += f"\n\n[Read {action['path']} — {result['lines']} lines]"
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": clean_text})
        save_history(self.history)
        return {
            "response": clean_text,
            "auto_executed": auto_executed,
            "pending_confirmation": pending_confirmation
        }

    def chat_agentic(self, user_message: str, max_iterations: int = 3) -> Generator[str, None, None]:
        self._maybe_compress()
        intent = self._classify_intent(user_message)

        import re
        from actions.filesystem import read_file as fs_read_file
        if intent == "executor":
            result = self.run_executor(user_message)
            yield result["response"]
            return

        if intent == "code" and self.context_enabled and self.indexer:
            initial_results = self.indexer.query(user_message, n_results=6)
            initial_context = "\n\n".join([
                f"[{r['metadata']['file_path'].split(chr(92))[-1]} | {r['metadata']['name']}]\n{r['text'][:400]}"
                for r in initial_results
            ]) if initial_results else "No initial context found."

            planning_messages = [
                {"role": "system", "content": f"""You are a code navigation assistant. Given a question and initial code snippets, identify which specific implementation files need to be read to answer fully.

    Rules:
    - Only request files you are confident exist based on the context
    - Prefer implementation files over __init__.py or README files
    - Maximum 3 files
    - If initial context is sufficient, return []
    - Return ONLY a JSON array of absolute Windows paths

    Initial context:
    {initial_context}"""},
                {"role": "user", "content": user_message}
            ]

            try:
                plan_response = self.client.chat(planning_messages, temperature=0.1, max_tokens=300)
                json_match = re.search(r'\[.*?\]', plan_response, re.DOTALL)
                files_to_read = json.loads(json_match.group()) if json_match else []
            except Exception:
                files_to_read = []

            extra_context_parts = []
            for path in files_to_read[:3]:
                result = fs_read_file(path)
                if result.get('success'):
                    content = result['content'][:2000]
                    fname = path.split('\\')[-1]
                    extra_context_parts.append(f"=== {fname} ===\n{content}")

            system = self.system_prompt

            # Add memory
            identity = load_identity()
            threads = get_threads_summary()
            memories = self.long_term.retrieve(user_message, n_results=2)
            if identity.get("name"):
                system += f"\n\nUser: {identity['name']}"
            if threads:
                system += f"\n\nOngoing threads:\n{threads}"
            if memories:
                memory_text = "\n".join([f"[{m['timestamp'][:10]}] {m['text']}" for m in memories])
                system += f"\n\nRelevant memories:\n{memory_text}"

            context_block = initial_context
            if extra_context_parts:
                context_block += "\n\n--- Files read ---\n\n" + "\n\n".join(extra_context_parts)

            trimmed_history = self.history[-4:] if len(self.history) > 4 else self.history
            messages = [{"role": "system", "content": system}] + trimmed_history
            messages.append({
                "role": "user",
                "content": f"Codebase context:\n\n{context_block}\n\nQuestion: {user_message}"
            })

        else:
            # unchanged fallback path
            messages = self._build_messages(user_message)

        full_response = self.client.chat(messages)
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_response})
        save_history(self.history)
        yield full_response

    def council_chat(self, user_message: str) -> dict:
        if not self.council:
            return {"error": "Council not available"}
        context = ""
        if self.context_enabled and self.indexer:
            results = self.indexer.query(user_message)
            if results:
                context = "\n\n".join([
                    f"[{r['metadata']['file_path'].split(chr(92))[-1]} | {r['metadata']['name']}]\n{r['text'][:400]}"
                    for r in results
                ])
        result = self.council.deliberate(user_message, context=context)
        self.history.append({"role": "user", "content": f"[COUNCIL] {user_message}"})
        self.history.append({"role": "assistant", "content": result["final"]})
        save_history(self.history)
        return result

    def set_project(self, project_name: str, indexer):
        self.enable_context(indexer)
        # Share the embedder to avoid loading twice
        self.long_term._external_embedder = indexer.embedder
        project_path = f"C:\\Users\\adnan\\Documents\\repos\\{project_name}"
        self.system_prompt = KAMIL_SYSTEM_PROMPT + f"""

Active project: {project_name}
Project directory: {project_path}
When writing files for this project, use paths inside: {project_path}
When the user says 'create a file' without specifying location, create it in the project directory."""
        save_setting("last_project", project_name)

    def enable_context(self, indexer):
        self.indexer = indexer
        self.context_enabled = True

    def reset(self):
        # Don't actually reset — just compress and continue
        if len(self.history) > 4:
            current_project = self.indexer.current_project if self.indexer else ""
            compress_messages(self.history, current_project)
        self.history = []
        clear_history()

    # ------------------ Voice‑optimised chat ------------------
    def chat_voice(self, user_message: str) -> Generator[dict, None, None]:
        """
        Two‑phase voice response:
        1. Small model for instant reply (with handoff detection)
        2. Primary model for complex queries
        """
        self._maybe_compress()

        # Lightweight context (identity + memory only, no code retrieval)
        identity = load_identity()
        threads = get_threads_summary()
        memories = self.long_term.retrieve(user_message, n_results=2)

        system = VOICE_SMALL_SYSTEM
        if identity.get("name"):
            system += f"\n\nUser's name: {identity['name']}"
        if threads:
            system += f"\n\nOngoing projects: {threads}"
        if memories:
            memory_text = "\n".join([f"[{m['timestamp'][:10]}] {m['text']}" for m in memories])
            system += f"\n\nRelevant memories:\n{memory_text}"

        trimmed_history = self.history[-4:] if len(self.history) > 4 else self.history
        messages = [{"role": "system", "content": system}] + trimmed_history
        messages.append({"role": "user", "content": user_message})

        # Phase 1 — fast response from small model
        try:
            quick_response = self.voice_client.chat(messages, max_tokens=60, temperature=0.3)
        except RuntimeError:
            # Voice model not running — go straight to primary
            quick_response = "let me check that for you"

        is_handoff = any(phrase in quick_response.lower() for phrase in VOICE_HANDOFF_PHRASES)

        if not is_handoff:
            # Small model answered fully — done
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": quick_response})
            save_history(self.history)
            yield {"phase": "final", "text": quick_response}
            return

        # Phase 2 — handoff to primary
        yield {"phase": "handoff", "text": quick_response}

        # Build full context for primary model (use _build_messages for code retrieval)
        full_messages = self._build_messages(user_message)
        # Add voice constraints for spoken response
        full_messages[0]["content"] += "\n\nIMPORTANT: This is a voice response. Keep your answer under 4 sentences. Be direct and spoken — no bullet points, no markdown, no lists."

        try:
            full_response = self.client.chat(full_messages, max_tokens=200, temperature=0.7)
        except RuntimeError as e:
            full_response = "Sorry, I couldn't reach the main model right now."

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_response})
        save_history(self.history)
        yield {"phase": "final", "text": full_response}