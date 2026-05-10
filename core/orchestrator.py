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
from memory.thread_state import get_thread_context_block, update_thread
from memory.long_term import LongTermMemory
from memory.compressor import compress_messages, should_compress
from memory.context_manager import get_context_manager
from typing import Generator
import json
import re
import os
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
        self.long_term = LongTermMemory()
        last_project = load_setting("last_project")
        self._last_project = last_project if last_project else None
        self._new_user = is_new_user()

        # Initialize context manager
        self.ctx = get_context_manager()

        # Load existing history into RAM on startup (last 10 messages)
        for msg in self.history[-10:]:
            self.ctx.add_message(msg)

    def _init_council(self):
        try:
            from core.council import Council
            self.council = Council()
        except Exception:
            self.council = None

    def _maybe_compress(self):
        if should_compress(self.history):
            to_compress = self.history[:8]
            current_project = self.indexer.current_project if self.indexer else ""
            compress_messages(to_compress, current_project)
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
            "code", "function", "class", "method", "file", "bug", "error",
            "import", "module", "implement", "write", "fix", "debug",
            "ekf", "prana", "navigation", "rover", "sensor", "check",
            ".py", "does it have", "list all", "what methods", "review",
            "folder", "directory", "variable", "does it", "how does", 
            "what does", "find", "inspect", "algorithm"
        ]

        if self.context_enabled and self.indexer:
            for kw in code_keywords:
                if kw in msg:
                    return "code"

        return "general"

    def _build_messages(self, user_message: str) -> list[dict]:
        intent = self._classify_intent(user_message)
        system = self.system_prompt

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

        if is_religious_query(user_message):
            religious_results = query_religious(user_message, n_results=3)
            if religious_results:
                context_text = "\n\n".join([
                    f"[{r['source']}, page {r['page']}]\n{r['text'][:400]}"
                    for r in religious_results
                ])
                system += f"\n\nRelevant Islamic sources — quote accurately from these, never fabricate verses:\n{context_text}"

        # Thread state injection — survives compression
        thread_block = get_thread_context_block()
        if thread_block:
            system += f"\n\n## ACTIVE THREAD\n{thread_block}"

        # Symbol-aware code context
        if self.context_enabled and self.indexer and self.indexer.current_project:
            intent = self._classify_intent(user_message)
            if intent in ("code", "general"):
                try:
                    from context.symbol_indexer import get_symbol_index
                    project_root = self.indexer._get_project_root(
                        self.indexer.current_project
                    )
                    if project_root:
                        sym_idx = get_symbol_index(
                            self.indexer.current_project,
                            project_root
                        )
                        if not sym_idx.is_indexed():
                            print(f"[orchestrator] Indexing symbols for {self.indexer.current_project}...")
                            sym_idx.index()
                        context = sym_idx.build_context_for_query(
                            user_message,
                            max_tokens=6000
                        )
                        if context:
                            system += f"\n\n## CODE CONTEXT\n{context}"
                except Exception as e:
                    print(f"[orchestrator] Symbol context error: {e}")
                    # Fallback to chunk retrieval
                    try:
                        chunks = self.indexer.embedder.query(
                            self.indexer.current_project,
                            user_message,
                            n_results=5
                        )
                        if chunks:
                            system += "\n\n## CODE CONTEXT\n"
                            for chunk in chunks:
                                system += f"\n```python\n{chunk['text']}\n```\n"
                    except Exception:
                        pass

        elif intent == "self":
            system += "\n\nThe user is asking about you or your memory. Answer from your knowledge about the user and your memory system. Do not reference code or project files."

        # Get context from manager (RAM + cache hits)
        context_messages = self.ctx.build_context(user_message)

        # Token guard
        total_chars = len(system) + sum(len(m.get("content","")) for m in context_messages)
        if total_chars > 24000:
            context_messages = self.ctx.ram[-4:]

        try:
            from actions.emcp_client import is_available, format_for_prompt
            from actions.emcp_client import _last_context
            if is_available() and _last_context:
                emcp_context = format_for_prompt(_last_context)
                if emcp_context:
                    system += f"\n\n{emcp_context}"
        except Exception:
            pass

        messages = [{"role": "system", "content": system}] + context_messages
        messages.append({"role": "user", "content": user_message})
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

        # Add to context manager
        self.ctx.add_message({"role": "user", "content": user_message})
        self.ctx.add_message({"role": "assistant", "content": full_response})

        # Detect if user is correcting Kamil
        correction_words = ["wrong", "incorrect", "that's not right", "no that's", 
                            "you're wrong", "mistake", "error", "not what i said",
                            "that's false", "incorrect"]
        if any(w in user_message.lower() for w in correction_words):
            from memory.sanity import record_correction
            record_correction()

        save_history(self.history)

    def chat(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        response = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": response})

        # Add to context manager
        self.ctx.add_message({"role": "user", "content": user_message})
        self.ctx.add_message({"role": "assistant", "content": response})

        save_history(self.history)
        return response

    def chat_with_actions(self, user_message: str) -> dict:
        from actions.tools import parse_tool_calls, execute_tool, requires_confirmation, get_tool_schemas
    
        self._maybe_compress()
        messages = self._build_messages(user_message)
        full_response = self.client.chat(messages)
    
        tool_calls = parse_tool_calls(full_response)
        import re
        clean_text = re.sub(r'```tool\s*\n.*?\n```', '', full_response, flags=re.DOTALL).strip()
    
        auto_executed = []
        pending_confirmation = []
    
        for call in tool_calls:
            tool_name = call["tool"]
            params = call.get("params", {})
        
            if requires_confirmation(tool_name):
                pending_confirmation.append(call)
            else:
                result = execute_tool(tool_name, params)
                auto_executed.append({"call": call, "result": result.to_dict()})
                if result.success and tool_name == "read_file":
                    clean_text += f"\n\n[Read {params.get('path', '')} — {result.output.get('lines', 0)} lines]"
    
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
            # ---- Index on demand if a .py file is mentioned ----
            file_matches = re.findall(r'(\w+)\.py', user_message, re.IGNORECASE)
            if file_matches:
                try:
                    structure = self.indexer.get_project_structure(
                        self.indexer.current_project
                    )
                    for fname in file_matches:
                        matches = [
                            f["path"] for f in structure.get("files", [])
                            if f["path"].lower().endswith(f"{fname.lower()}.py")
                        ]
                        for rel_path in matches[:2]:
                            full_path = os.path.join(
                                structure["root"], rel_path
                            )
                            self.indexer.index_file_on_demand(
                                full_path, 
                                self.indexer.current_project
                            )
                except Exception as e:
                    print(f"[agentic] Index-on-demand failed: {e}")
            # ----------------------------------------------------
            try:
                from context.symbol_indexer import get_symbol_index
                project_root = self.indexer._get_project_root(
                    self.indexer.current_project
                )
                if project_root:
                    sym_idx = get_symbol_index(
                        self.indexer.current_project,
                        project_root
                    )
                    if not sym_idx.is_indexed():
                        print(f"[agentic] Indexing symbols...")
                        sym_idx.index()
                    
                    code_context = sym_idx.build_context_for_query(
                        user_message,
                        max_tokens=6000
                    )
                    print(f"[agentic] Symbol context: {len(code_context)} chars")
                else:
                    code_context = "No project root found."
            except Exception as e:
                print(f"[agentic] Symbol context error: {e}")
                import traceback
                traceback.print_exc()
                code_context = "Symbol index unavailable."

            system = self.system_prompt
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

            trimmed_history = self.history[-4:] if len(self.history) > 4 else self.history
            messages = [{"role": "system", "content": system}] + trimmed_history
            messages.append({
                "role": "user",
                "content": f"## CODE CONTEXT\n{code_context}\n\nQuestion: {user_message}"
            })

        else:
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
        if len(self.history) > 4:
            current_project = self.indexer.current_project if self.indexer else ""
            compress_messages(self.history, current_project)
        self.history = []
        clear_history()
        self.ctx.clear()

    def chat_voice(self, user_message: str) -> Generator[dict, None, None]:
        self._maybe_compress()

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

        try:
            quick_response = self.voice_client.chat(messages, max_tokens=60, temperature=0.3)
        except RuntimeError:
            quick_response = "let me check that for you"

        is_handoff = any(phrase in quick_response.lower() for phrase in VOICE_HANDOFF_PHRASES)

        if not is_handoff:
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": quick_response})
            save_history(self.history)
            yield {"phase": "final", "text": quick_response}
            return

        yield {"phase": "handoff", "text": quick_response}

        full_messages = self._build_messages(user_message)
        full_messages[0]["content"] += "\n\nIMPORTANT: This is a voice response. Keep your answer under 4 sentences. Be direct and spoken — no bullet points, no markdown, no lists."

        try:
            full_response = self.client.chat(full_messages, max_tokens=200, temperature=0.7)
        except RuntimeError as e:
            full_response = "Sorry, I couldn't reach the main model right now."

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_response})
        save_history(self.history)
        yield {"phase": "final", "text": full_response}