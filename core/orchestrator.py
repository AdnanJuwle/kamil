# orchestrator.py
from models.client import LlamaClient
from core.config import (
    KAMIL_SYSTEM_PROMPT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS,
    LLAMA_SERVER_URL, VOICE_SERVER_URL
)
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
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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

    # _classify_banter, _chat_dolphin, BANTER_CLASSIFIER_PROMPT removed

    def chat_stream(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
        # Banter check removed – all messages go through normal processing
        yield from self.chat_stream_internal(user_message, temperature, max_tokens)

    def chat_stream_internal(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        full_response = ""
        for chunk in self.client.chat_stream(messages, temperature=temperature, max_tokens=max_tokens):
            full_response += chunk
            yield chunk
        # Kamil can save to its own memory mid-chat via a trailing tool block.
        full_response = self._apply_memory_ops(full_response)
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

    def _apply_memory_ops(self, text: str) -> str:
        """Execute any remember/update_memory tool calls Kamil put in its reply,
        and strip those blocks from what gets shown/saved. Lets Kamil curate its
        own memory.md during normal (non-agent) chat."""
        try:
            from actions.tools import parse_tool_calls, execute_tool
            from actions.agent import strip_tool_blocks
            calls = [c for c in parse_tool_calls(text)
                     if c.get("tool") in ("remember", "update_memory", "forget")]
            if not calls:
                return text
            for c in calls:
                try:
                    execute_tool(c["tool"], c["params"])
                except Exception:
                    pass
            return strip_tool_blocks(text)
        except Exception:
            return text

    def _get_schedule_context(self) -> str:
        from core.schedule import schedule_context
        return schedule_context()

    def proactive_message(self, reason: str, fallback: str) -> str:
        """Phrase an unprompted ping in Kamil's voice. Falls back to `fallback`
        (the engine's templated text) if the model is unavailable."""
        system = (
            self.system_prompt +
            "\n\nYou are reaching out to Adnan UNPROMPTED right now — he did not just "
            f"message you. Reason: {reason}\n"
            "Say ONE short, natural line, like a friend pinging him. No greeting "
            "boilerplate, no markdown, no lists. Max two sentences."
        )
        try:
            line = self.client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": "(reach out now)"}],
                temperature=0.7, max_tokens=80,
            )
            line = (line or "").strip().strip('"')
            return line or fallback
        except Exception:
            return fallback

    def _build_messages(self, user_message: str) -> list[dict]:
        greeting_patterns = ["hello", "hi", "hey", "good morning", "good evening", 
                             "salam", "assalamu", "what's up", "sup"]
        is_greeting = any(user_message.lower().strip().startswith(g) 
                          for g in greeting_patterns) and len(user_message.split()) < 5

        if is_greeting:
            system_base = self.system_prompt
            messages = [{"role": "system", "content": system_base}]
            messages.append({"role": "user", "content": user_message})
            return messages

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

        schedule_ctx = self._get_schedule_context()
        system += f"\n\n## SCHEDULE\n{schedule_ctx}"
        system += "\nIf Adnan is chatting during a deep work block, gently acknowledge it. Don't lecture, just be aware."
        system += "\nWhen mentioning schedule — be direct, no motivational padding, no 'optimal productivity' language."

        # Temporal continuity: note how long since Adnan last spoke, then stamp this
        # interaction so the heartbeat knows he's present.
        try:
            from core import presence
            from datetime import datetime as _dt
            note = presence.temporal_note(presence.get_last_interaction(), _dt.now())
            if note:
                system += f"\n\n{note}"
            presence.record_interaction()
        except Exception:
            pass

        # Kamil's own self-authored memory notes (it curates these via tools).
        try:
            from core.self_memory import memory_for_prompt, is_over_cap
            notes = memory_for_prompt()
            if notes:
                system += f"\n\n## WHAT I'VE LEARNED (my own memory — I keep and curate this)\n{notes}"
                if is_over_cap():
                    system += "\n(These notes are getting long — consolidate them with update_memory.)"
        except Exception:
            pass

        if is_religious_query(user_message):
            religious_results = query_religious(user_message, n_results=3)
            if religious_results:
                context_text = "\n\n".join([
                    f"[{r['source']}, page {r['page']}]\n{r['text'][:400]}"
                    for r in religious_results
                ])
                system += f"\n\nRelevant Islamic sources — quote accurately from these, never fabricate verses:\n{context_text}"

        thread_block = get_thread_context_block()
        if thread_block:
            system += f"\n\n## ACTIVE THREAD\n{thread_block}"

        if self.indexer and not self.indexer.current_project:
            available = self.indexer.list_projects()
            msg_lower = user_message.lower()
            for project in available:
                if project.lower() in msg_lower:
                    print(f"[orchestrator] Project mention detected: {project} — scanning")
                    self.indexer.scan_project_structure(project)
                    self.indexer.current_project = project
                    self.context_enabled = True
                    break

        available_projects = self.indexer.list_projects() if self.indexer else []
        if available_projects:
            system += f"\n\nAvailable projects: {', '.join(available_projects)}"
            if self.indexer and self.indexer.current_project:
                system += f"\nCurrently loaded: {self.indexer.current_project}"
            else:
                system += "\nNo project loaded. Will auto-load when user mentions a project name."

        # Domain/skill retrieval: always query the library and let SIMILARITY decide
        # (the old keyword gate blocked ~40% of valid queries; the 0.55 cutoff sat
        # above the on-topic band). Calibrated: on-topic >=0.50, off-topic <=0.38,
        # so 0.45 cleanly separates. No hard domain filter — let score rank across all.
        from context.domain_indexer import query_domain
        try:
            domain_results = query_domain(
                user_message,
                n_results=3,
                domain_filter=None,
                prefer_operational=(intent == "code"),
            )
            domain_results = [r for r in domain_results if r.get("score", 0) >= 0.45]
            if domain_results:
                domain_text = "\n\n".join([
                    f"[{r['source']}, {r['domain']}, p{r['page']}]\n{r['text'][:500]}"
                    for r in domain_results
                ])
                system += f"\n\nRelevant knowledge from your library (cite the source):\n{domain_text}"
                print(f"[orchestrator] Domain context: {len(domain_results)} chunks "
                      f"(top score {domain_results[0]['score']:.2f})")
        except Exception as e:
            print(f"[orchestrator] Domain retrieval error: {e}")

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

        context_messages = self.ctx.build_context(user_message)
        if len(context_messages) > 4:
            context_messages = context_messages[-4:]

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

        # Sealed succession directive. Activation is voice-only (the heir must
        # SAY the word — handled in the /transcribe path); here we only inject
        # whatever is already in force. scan_typed_test is a no-op unless
        # KAMIL_SUCCESSION_TEST=1, letting Adnan dry-run it from text.
        try:
            from core.succession import scan_typed_test, succession_context
            scan_typed_test(user_message)
            succ = succession_context()
            if succ:
                system += f"\n\n{succ}"
        except Exception:
            pass

        messages = [{"role": "system", "content": system}] + context_messages
        messages.append({"role": "user", "content": user_message})
        return messages

    def chat(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        self._maybe_compress()
        messages = self._build_messages(user_message)
        response = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
        response = self._apply_memory_ops(response)
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": response})
        self.ctx.add_message({"role": "user", "content": user_message})
        self.ctx.add_message({"role": "assistant", "content": response})
        save_history(self.history)
        return response

    def chat_agent(self, user_message: str, max_iters: int = 6) -> Generator[dict, None, None]:
        """
        Agentic tool-use loop: the model can call tools, see their results, and
        continue across multiple steps until the task is done. Yields structured
        events (see actions/agent.py). Mutating tools pause for confirmation.
        """
        from actions.agent import iter_agent
        from actions.tools import get_tools_for_prompt

        self._maybe_compress()
        messages = self._build_messages(user_message)
        messages[0]["content"] += (
            "\n\n## AGENT MODE\nYou can use tools across multiple steps. Available tools:\n"
            + get_tools_for_prompt()
            + "\n\nTo call a tool, output a fenced block exactly like:\n"
            '```tool\n{"tool": "read_file", "params": {"path": "C:\\\\path\\\\file.py"}}\n```\n'
            "After each tool runs you will see its result, then call more tools or give "
            "your final answer with NO tool block when finished. Tools marked "
            "[needs approval] will pause for the user. Prefer edit_file over write_file "
            "for changing existing files.\n"
            "Rules: never write out or guess a tool's result before you run it — call "
            "the tool and use the REAL result. Don't repeat a tool call you already "
            "made. Once a tool gives you what you need, ANSWER from it directly — never "
            "say 'we need to look at the file' after you've already read it."
        )

        model_fn = lambda msgs: self.client.chat(msgs)
        final_text = ""
        terminal = "done"
        for event in iter_agent(model_fn, messages, max_iters=max_iters):
            if event.get("type") in ("text", "done", "max_iterations") and event.get("text"):
                final_text = event["text"]
            terminal = event.get("type", terminal)
            yield event

        # Persist the turn once it completes (not while paused for confirmation).
        if terminal in ("done", "max_iterations") and final_text:
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": final_text})
            self.ctx.add_message({"role": "user", "content": user_message})
            self.ctx.add_message({"role": "assistant", "content": final_text})
            save_history(self.history)

    def resume_agent(self, transcript: list[dict], executed: list, user_message: str = "",
                     max_iters: int = 6) -> Generator[dict, None, None]:
        """
        Continue an agent loop that paused for confirmation. `executed` is a list
        of (call, result_dict) the caller ran after the user approved/denied them.
        Yields the same event stream as chat_agent.
        """
        from actions.agent import resume_agent as _resume

        model_fn = lambda msgs: self.client.chat(msgs)
        final_text = ""
        terminal = "done"
        for event in _resume(model_fn, transcript, executed, max_iters=max_iters):
            if event.get("type") in ("text", "done", "max_iterations") and event.get("text"):
                final_text = event["text"]
            terminal = event.get("type", terminal)
            yield event

        if terminal in ("done", "max_iterations") and final_text:
            label = user_message or "[agent task]"
            self.history.append({"role": "user", "content": label})
            self.history.append({"role": "assistant", "content": final_text})
            self.ctx.add_message({"role": "user", "content": label})
            self.ctx.add_message({"role": "assistant", "content": final_text})
            save_history(self.history)

    def chat_agentic(self, user_message: str, max_iterations: int = 3) -> Generator[str, None, None]:
        self._maybe_compress()
        intent = self._classify_intent(user_message)

        import re
        if self.indexer and not self.indexer.current_project:
            available = self.indexer.list_projects()
            msg_lower = user_message.lower()
            for project in available:
                if project.lower() in msg_lower:
                    print(f"[orchestrator] Project mention detected: {project} — scanning")
                    self.indexer.scan_project_structure(project)
                    self.indexer.current_project = project
                    self.context_enabled = True
                    break

        if intent == "code" and self.context_enabled and self.indexer and self.indexer.current_project:
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