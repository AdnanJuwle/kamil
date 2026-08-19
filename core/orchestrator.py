# orchestrator.py
from models.client import LlamaClient
from core.config import (
    KAMIL_SYSTEM_PROMPT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS,
    LLAMA_SERVER_URL, VOICE_SERVER_URL, PROJECTS_ROOT
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

def _agent_needs_confirm():
    """The confirmation predicate for the agent loop. Default (KAMIL_AGENT_AUTO=1):
    in-fence mutations auto-run, out-of-fence pause. KAMIL_AGENT_AUTO=0: confirm
    every mutation (the old behaviour)."""
    from actions.tools import requires_confirmation, requires_confirmation_call
    if os.getenv("KAMIL_AGENT_AUTO", "1") == "1":
        return requires_confirmation_call
    return requires_confirmation


import re as _re_task
_TASK_PATTERNS = [
    r"\b(today|current|currently|latest|right now|recent(ly)?|price|prices|stock|weather|news|score|exchange rate|who won|as of)\b",
    r"\b(create|write|make|build|generate|run|execute|fix|edit|change|add|implement|refactor|delete|remove|install|rename|move|append|look up|search for|google|find me|open the|read the|list the|grep|show me the|go through|check the|scan the|inspect|debug)\b",
    r"\.(py|js|ts|jsx|tsx|md|json|txt|html|css|java|cpp|c|rs|go|sh|yml|yaml|toml|ino)\b",
    r"\b(folder|directory|repo|repository|codebase|file|files|function|functions|class|classes|algorithm|algorithms|the code|which functions|what methods)\b",
    r"\b(look at|take a look|what am i looking at|what do you see|can you see|what'?s in front of me|what'?s on (my|the) desk|use your (eyes|camera|webcam)|through the (camera|webcam)|check the (camera|webcam|image|photo|picture|screen)|describe (this|the|my) (image|photo|picture|screen|frame)|read (this|the) (sign|label|screen|text))\b",
]


def looks_like_task(text: str) -> bool:
    """Heuristic: does this utterance want TOOLS (web/files/vision/actions), or is it
    just conversation? Mirrors the frontend router so the voice call doesn't feed a
    greeting to the tool agent (which then invents a coding task)."""
    s = " " + _re_task.sub(r"\s+", " ", _re_task.sub(r"[^a-z0-9'./\- ]", " ", (text or "").lower())) + " "
    return any(_re_task.search(p, s) for p in _TASK_PATTERNS)


def _log_agent_event(event: dict) -> None:
    """Record executed tool calls to the persistent action log (for the Work tab)."""
    if event.get("type") == "tool_result":
        try:
            from core import action_log
            r = event.get("result") or {}
            action_log.record_tool(event.get("tool"), r.get("params"),
                                    ok=event.get("success"),
                                    detail="" if event.get("success") else str(r.get("error", ""))[:120])
        except Exception:
            pass


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


_ACTIVE_ORCH = None   # the live Orchestrator, so tools can reach its indexer


def get_orchestrator():
    """The live Orchestrator instance (set on construction) — used by tools that
    need the active project indexer, e.g. open_project."""
    return _ACTIVE_ORCH


class Orchestrator:
    def __init__(self):
        self.client = LlamaClient(base_url=LLAMA_SERVER_URL)
        self.voice_client = LlamaClient(base_url=VOICE_SERVER_URL)
        self.history = load_history()
        self._project_block = ""   # appended to the hot-reloaded base prompt
        self.indexer = None
        self.context_enabled = False
        self.council = None
        self._init_council()
        self.long_term = LongTermMemory()
        last_project = load_setting("last_project")
        self._last_project = last_project if last_project else None
        global _ACTIVE_ORCH
        _ACTIVE_ORCH = self   # so tools (open_project) can reach the live indexer
        self._new_user = is_new_user()

        # Initialize context manager
        self.ctx = get_context_manager()

        # Load existing history into RAM on startup (last 10 messages)
        for msg in self.history[-10:]:
            self.ctx.add_message(msg)

    @property
    def system_prompt(self) -> str:
        # Hot-reloaded base prompt (edits to system.md apply next message, no
        # restart) plus any active-project addendum.
        from core.config import get_system_prompt
        return get_system_prompt() + self._project_block

    def _init_council(self):
        try:
            from core.council import Council
            self.council = Council()
        except Exception:
            self.council = None

    def _maybe_compress(self):
        # Keep a long rolling window in active context (we have a 32k window); only
        # archive the OLDEST turns to long-term memory once history gets big, so
        # Kamil actually remembers the current conversation instead of forgetting
        # after ~4 exchanges. Tunable via env.
        limit = int(os.getenv("KAMIL_HISTORY_LIMIT", "50"))
        retain = int(os.getenv("KAMIL_HISTORY_RETAIN", "36"))
        if len(self.history) >= limit:
            n = max(1, len(self.history) - retain)   # archive oldest, keep `retain` recent
            current_project = self.indexer.current_project if self.indexer else ""
            compress_messages(self.history[:n], current_project)
            self.history = self.history[n:]
            save_history(self.history)

    def _cap_prompt(self, messages: list[dict], max_chars: int) -> list[dict]:
        """Keep the total prompt under max_chars by dropping the OLDEST middle
        messages (never the system prompt [0] or the final user message [-1])."""
        def total():
            return sum(len(m.get("content", "")) for m in messages)
        # drop from index 1 upward (oldest history) until it fits
        while total() > max_chars and len(messages) > 2:
            del messages[1]
        # if still too big, it's the system prompt itself (huge code context) — trim it
        if total() > max_chars and messages:
            over = total() - max_chars
            sys = messages[0].get("content", "")
            if len(sys) > over + 2000:
                messages[0]["content"] = sys[: len(sys) - over - 200] + "\n…(context trimmed)"
        return messages

    def _agent_tool_instructions(self) -> str:
        """The tool-use rules shared by both agent tiers."""
        from actions.tools import get_tools_for_prompt
        return (
            "## AGENT MODE\nYou can use tools across multiple steps. Available tools:\n"
            + get_tools_for_prompt()
            + "\n\nTo call a tool, output a fenced block exactly like:\n"
            '```tool\n{"tool": "read_file", "params": {"path": "C:\\\\path\\\\file.py"}}\n```\n'
            "After each tool runs you will see its result, then call more tools or give your "
            "final answer with NO tool block. Prefer edit_file over write_file for existing files.\n"
            "Work autonomously: edit_file/write_file/shell run automatically inside the "
            "workspace or a project repo (non-destructive) — only actions outside those bounds "
            "pause. For a coding task: read → edit → run a check → fix → report. Don't ask "
            "permission for normal in-project edits.\n"
            "Never guess a tool's result before running it. Don't repeat a call you already made. "
            "Once a tool gives you what you need, ANSWER from it.\n"
            "CREATING FILES — CRITICAL: to create/save/write a file you MUST call write_file in a "
            "```tool block. Printing code in a ```python block does NOT create a file. NEVER claim "
            "a file is created/stored without a real write_file success result. Put the plain code "
            "in write_file's content (no ``` fences, real newlines). Do NOT also print the code "
            "outside the tool call — it wastes the output budget and can cut the call off. Emit the "
            "```tool block directly.\n"
            "Use the FULL absolute path inside the project directory when writing files "
            "(e.g. the Project directory shown below), not a bare relative name.\n"
            "PROJECT FIRST: if the task edits or creates files in a project and NO project is "
            "loaded (no Project directory shown below), your FIRST tool call is list_projects, "
            "then open_project with the right name — BEFORE any write_file/edit_file/shell. Never "
            "guess or invent a path (e.g. C:\\Users\\...\\SomeProject) and never write to a scratch "
            "folder as a substitute; if a write is refused for being outside the project, open the "
            "correct project and retry inside it. Once a project is loaded, writes are confined to "
            "its directory.\n"
            "AFTER writing a file: verify it with EXACTLY `python -m py_compile <full_path>` "
            "(that catches syntax errors). Then you are DONE — report the path and how to run it. "
            "NEVER run the file itself with shell: GUI apps (tkinter/PyQt) and servers BLOCK/hang "
            "with no display, so running them only fails and burns turns. If a run fails, do NOT "
            "retry it — py_compile instead and stop. Do not loop on shell failures.\n"
            "shell is for READ-ONLY inspection (ls, cat, grep, git status) and mkdir ONLY — never "
            "write files with shell (no > redirects, heredocs, tee, sed -i); create/change files "
            "ONLY with write_file/edit_file.\n"
            "LONG commands (pip install, npm install, flutter pub get, builds, git clone, downloads) "
            "take minutes — call shell with \"background\": true so they don't hit the timeout. You "
            "get a job_id back immediately; keep working, and check it later with job_status if you "
            "need the outcome. Don't sit and wait on a background job.\n"
            "When a tool FAILS, do not go silent or give up: retry with a corrected call, or say "
            "exactly what's blocking you and what you'll try next. A failed tool is not a stopping "
            "condition — only a successful one is.\n"
            "NOT FOUND: if a file or thing you're asked about isn't where expected, check ONE likely "
            "alternative at most, then STOP and tell the user you couldn't find it (name what you "
            "looked for). Do NOT crawl the whole project directory-by-directory — a wrong or "
            "misheard name is common; say so quickly instead of searching everywhere.\n"
            "YOUR OWN SOURCE: you (Kamil) are a Python/FastAPI system at C:\\kamil. When asked "
            "about your own code/source/architecture, READ files under C:\\kamil (core\\, actions\\, "
            "interfaces\\, models\\) — that's the real you. It is NOT Kamil_v1/Kamil_v2 in "
            "Documents\\repos (those are scratch projects). You can read your own code but not "
            "modify it; code13\\ is sealed. Never invent a 'get_code' tool — just read the files.\n"
            "VISION: when asked what you see / to look / open the camera, you MUST call the look "
            "tool FRESH every single time — even if you looked a moment ago. The scene changes; "
            "never answer from a previous frame or from memory. Report only what THIS look returns.\n"
            "CURRENT INFO: your training stops at 2023, so for ANY recent/news/price/'latest' "
            "question your FIRST action is web_search, then answer from what you found. Use it."
        )

    def _project_code_context(self, user_message: str) -> str:
        """Project directory pointer + relevant symbol-indexed code, for the coder."""
        if not (self.context_enabled and self.indexer and self.indexer.current_project):
            return ""
        proj = self.indexer.current_project
        proj_dir = os.path.join(PROJECTS_ROOT, proj)
        header = (f"## PROJECT\nCurrent project: {proj}\n"
                  f"Project directory: {proj_dir}\n"
                  f"Write EVERY file with a full path inside {proj_dir}. If the user asks for 'its own "
                  f"folder', make a SUBFOLDER inside the project (e.g. {proj_dir}\\medical_gui\\main.py) "
                  f"and create it with `mkdir` first. NEVER write to the Desktop, home directory, or any "
                  f"path outside {proj_dir} — those need approval and usually don't exist.")
        try:
            from context.symbol_indexer import get_symbol_index
            root = self.indexer._get_project_root(proj)
            if root:
                sym = get_symbol_index(proj, root)
                if not sym.is_indexed():
                    sym.index()
                ctx = sym.build_context_for_query(user_message, max_tokens=5000)
                if ctx:
                    return header + "\n\n## CODE CONTEXT\n" + ctx
        except Exception as e:
            print(f"[orchestrator] code context error: {e}")
        return header

    def _asks_about_own_source(self, text: str) -> bool:
        """True when Adnan is asking about KAMIL'S OWN code/source/architecture (which
        lives at C:\\kamil), not about a project in Documents\\repos."""
        t = (text or "").lower()
        # direct "how are/were you built/made/designed" style self-inquiries
        if any(p in t for p in (
                "how are you built", "how were you built", "how are you made",
                "how were you made", "how you are built", "how you're built",
                "how do you work", "how you work", "what are you made of",
                "how are you designed", "how were you designed")):
            return True
        code_word = any(w in t for w in (
            "source", "code", "codebase", "architecture", "internals",
            "implementation", "your files"))
        self_word = any(w in t for w in ("your", "yourself", "you're"))
        return code_word and self_word

    def _self_source_context(self, user_message: str) -> str:
        """Point the agent at Kamil's OWN source (read-only) when asked about itself."""
        root = "C:\\kamil"
        return (
            "## YOUR OWN SOURCE\n"
            f"You (Kamil) are a local Python/FastAPI system whose source lives at {root}. "
            "This is literally YOU — not a project in Documents\\repos (Kamil_v1 / Kamil_v2 are "
            "unrelated scratch projects, NOT your code). To answer questions about yourself or "
            "suggest improvements, READ your real files:\n"
            "  - core\\        orchestrator, memory, model tiers, config\n"
            "  - actions\\     your tools + the agentic loop\n"
            "  - interfaces\\  the web app (FastAPI backend + UI)\n"
            "  - models\\      the llama.cpp client\n"
            f"Use read_file / list_directory with full paths under {root}. You CAN read your own "
            "code but you CANNOT modify it — changes to your own brain go through Adnan, and "
            "code13\\ is sealed and off-limits. There is no 'get_code' tool; never invent one — "
            "just read the files. Ground every claim about yourself in a file you actually read.\n"
            f"NEVER call edit_file or write_file on any path under {root} — your own source is "
            "read-only and every such write is refused anyway. Deliver improvements as TEXT "
            "(what to change and why); do NOT attempt an edit 'just in case'.\n"
            "REVIEWING A FILE: read it with ONE read_file call using its full path "
            f"(e.g. read_file {root}\\core\\orchestrator.py) — one read returns ~600 lines. If it is "
            "truncated, read the remaining region ONCE more with read_lines; do not page through it "
            "in tiny chunks. Then base your critique ONLY on what you actually read: cite real "
            "function/class names and quote the actual code. Do NOT offer generic advice (\"add "
            "error handling\", \"add docstrings\", \"add tests\", \"avoid hardcoded passwords\") "
            "unless you SAW that specific gap — this code already has extensive docstrings and error "
            "handling, so boilerplate suggestions are wrong and unhelpful. Give 2-4 concrete, "
            "specific improvements tied to real code you read.")

    def _coding_agent_messages(self, user_message: str, tool_instr: str,
                               own_source: bool = False) -> list[dict]:
        """A clean, persona-free prompt for the aligned coding model."""
        system = ("You are an expert software engineer and coding agent working inside Adnan's "
                  "local project workspace on his own machine. Complete his coding request "
                  "precisely and practically: write correct, complete, runnable code and use your "
                  "tools to do it. Keep prose brief.\n\n" + tool_instr)
        ctx = self._self_source_context(user_message) if own_source else self._project_code_context(user_message)
        if ctx:
            system += "\n\n" + ctx
        recent = [m for m in getattr(self.ctx, "ram", [])[-6:]
                  if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
        return [{"role": "system", "content": system}] + recent + [{"role": "user", "content": user_message}]

    def _looks_like_coding(self, message: str) -> bool:
        """Does this task actually need the big coding model? (vs. a web lookup or
        file read the 8B can drive.)"""
        s = message.lower()
        kw = ["code", "gui", "script", "program", "function", "class ", "method",
              "refactor", "implement", "debug", "compile", "module", "api ",
              "endpoint", "write a ", "create a file", "create a script",
              "create a gui", "create a demo", "make a file", "edit the", "edit ",
              "fix the", "add a ", "build a", "rewrite", "unit test", "the bug",
              ".py", ".js", ".ts", ".html", ".css", ".java", ".cpp", ".ino"]
        return any(k in s for k in kw)

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

        # Kamil curates its own memory.md in the background — notices durable facts
        # and saves them without being asked (throttled, deduped, non-blocking).
        try:
            from core import memory_reflect
            memory_reflect.maybe_reflect(
                self.history, lambda m: self.client.chat(m, max_tokens=300, temperature=0.2))
        except Exception as _e:
            print(f"[memory] reflect hook error: {_e}")

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
        # Anti-hallucination for live facts in plain replies (agent mode overrides
        # this with a web_search directive): never invent current prices, rates,
        # scores, news, or "today" facts — say you'll look it up instead.
        system += ("\n\nLIVE FACTS: You do not have live internet in a plain reply. For anything "
                   "current — prices, exchange rates, scores, news, today's date-specific facts — do "
                   "NOT make up a number or claim you searched. Say you'll look it up, and the user "
                   "can ask you to. Never state a figure as current unless it came from a real search.")

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

        # Auto-load / auto-switch: if the message names a known project, open it
        # (switching even when another is already loaded). Kamil can also open one
        # deliberately via the open_project tool.
        if self.indexer:
            mentioned = self.resolve_project(user_message)
            if mentioned and mentioned != self.indexer.current_project:
                print(f"[orchestrator] Project named: {mentioned} — auto-switching")
                self.open_project(mentioned)

        available_projects = self.indexer.list_projects() if self.indexer else []
        if available_projects:
            system += f"\n\nAvailable projects: {', '.join(available_projects)}"
            if self.indexer and self.indexer.current_project:
                system += f"\nCurrently loaded: {self.indexer.current_project}"
            else:
                system += ("\nNo project loaded. Name a project to load it, or use the "
                           "open_project tool when a task needs one.")

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

        # Keep a long conversational window in the prompt (32k context) so Kamil
        # remembers this conversation, not just the last couple of turns.
        ctx_keep = int(os.getenv("KAMIL_CONTEXT_MSGS", "30"))
        ctx_budget = int(os.getenv("KAMIL_CONTEXT_CHARS", "60000"))
        context_messages = self.ctx.build_context(user_message)
        if len(context_messages) > ctx_keep:
            context_messages = context_messages[-ctx_keep:]

        total_chars = len(system) + sum(len(m.get("content","")) for m in context_messages)
        while total_chars > ctx_budget and len(context_messages) > 4:
            context_messages = context_messages[2:]   # drop oldest, keep the tail
            total_chars = len(system) + sum(len(m.get("content","")) for m in context_messages)

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

    def chat_agent(self, user_message: str, max_iters: int = None, voice: bool = False) -> Generator[dict, None, None]:
        """
        Agentic tool-use loop: the model can call tools, see their results, and
        continue across multiple steps until the task is done. Yields structured
        events (see actions/agent.py). Mutating tools pause for confirmation.
        `voice=True` tells it the final answer will be spoken aloud, so keep it short.
        """
        from actions.agent import iter_agent, iter_agent_native
        from actions.tools import get_tools_openai_schema

        if max_iters is None:
            max_iters = int(os.getenv("KAMIL_AGENT_MAX_ITERS", "10"))
        if voice:
            # A spoken task should be quick; cap iterations so a tool-confused small
            # model can't spiral for minutes on a simple request.
            max_iters = min(max_iters, int(os.getenv("KAMIL_VOICE_MAX_ITERS", "5")))

        self._maybe_compress()
        # Load the project the user named. The coding path uses a clean prompt (not
        # _build_messages, where auto-switch normally lives), so without this the
        # coder gets NO project directory and guesses a random folder (Desktop).
        own_source = False
        if self.indexer:
            mentioned = self.resolve_project(user_message)
            # A question about Kamil's OWN code points at C:\kamil, not a repos project.
            own_source = self._asks_about_own_source(user_message) and not mentioned
            if own_source:
                print("[orchestrator] self-source question — pointing at C:\\kamil (read-only)")
            elif mentioned and mentioned != self.indexer.current_project:
                print(f"[orchestrator] Project named: {mentioned} — auto-switching (agent)")
                self.open_project(mentioned)
        tool_instr = self._agent_tool_instructions()
        voice_note = ("\n\n## SPOKEN ANSWER\nThis is a voice call. Do the work with tools, then "
                      "give your FINAL answer in at most 2-3 short spoken sentences — no markdown, "
                      "no lists, no code. Summarise what you did; offer details on screen if long.")

        # Decide the coding tier FIRST — the aligned coder needs a clean prompt.
        agent_max = int(os.getenv("KAMIL_AGENT_MAX_TOKENS", "6000"))
        agent_client = self.client   # fast 8B by default
        agent_temp = 0.7
        agent_sampling = None
        _heavy = None
        # Voice can use the 30B coder too (the "freeze" was VRAM contention with the
        # resident Chatterbox tier — fixed in heavy_tier.plan(); once fit it runs fast).
        if os.getenv("KAMIL_AGENT_HEAVY", "1") == "1" and self._looks_like_coding(user_message):
            try:
                from core import heavy_tier
                if heavy_tier.available():
                    yield {"type": "status", "text": "spinning up the coding model…"}
                    if heavy_tier.ensure_loaded():
                        from models.client import LlamaClient
                        agent_client = LlamaClient(base_url=heavy_tier.HEAVY_URL)
                        # Low temp + repetition penalty: better code, no looping on the
                        # quantized MoE port (Odysseus does the same for local MoE).
                        agent_temp = 0.25
                        agent_sampling = {"repeat_penalty": 1.1, "top_p": 0.9, "top_k": 40}
                        _heavy = heavy_tier
            except Exception as _e:
                print(f"[orchestrator] heavy tier unavailable: {_e}")

        if _heavy is not None:
            # Clean coding-agent prompt. Kamil's 240-line persona system prompt
            # ("no content policy", political/religious framing, "uncensored") makes
            # the ALIGNED coder refuse benign tasks. The coder is a tool — give it a
            # focused coding brief + the project's code + the tools, nothing else.
            messages = self._coding_agent_messages(user_message, tool_instr + (voice_note if voice else ""),
                                                   own_source=own_source)
        else:
            # 8B chat model (lexi, uncensored) keeps Kamil's full persona.
            messages = self._build_messages(user_message)
            if voice:
                messages[0]["content"] += voice_note
            messages[0]["content"] += "\n\n" + tool_instr

        # Hard cap so the prompt can't overflow the window and 400.
        messages = self._cap_prompt(messages, max_chars=int(os.getenv("KAMIL_AGENT_PROMPT_CHARS", "88000")))

        # NATIVE tool-calling by default: pass the tool schema so the model returns
        # structured tool_calls (reliable) instead of a hand-formatted ```tool fence.
        # Falls back to text parsing inside the loop when the server doesn't emit them.
        if os.getenv("KAMIL_NATIVE_TOOLS", "1") == "1":
            tools_schema = get_tools_openai_schema()
            chat_fn = lambda msgs: agent_client.chat_tools(
                msgs, tools_schema, temperature=agent_temp, max_tokens=agent_max, sampling=agent_sampling)
            loop = iter_agent_native(chat_fn, messages, max_iters=max_iters, needs_confirm=_agent_needs_confirm())
        else:
            model_fn = lambda msgs: agent_client.chat(
                msgs, temperature=agent_temp, max_tokens=agent_max, sampling=agent_sampling)
            loop = iter_agent(model_fn, messages, max_iters=max_iters, needs_confirm=_agent_needs_confirm())

        final_text = ""
        terminal = "done"
        for event in loop:
            _log_agent_event(event)
            if event.get("type") in ("text", "done", "max_iterations") and event.get("text"):
                final_text = event["text"]
            terminal = event.get("type", terminal)
            yield event

        if _heavy is not None:
            _heavy.mark_activity()   # keep it warm for follow-up coding turns

        # Persist the turn once it completes (not while paused for confirmation).
        if terminal in ("done", "max_iterations") and final_text:
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": final_text})
            self.ctx.add_message({"role": "user", "content": user_message})
            self.ctx.add_message({"role": "assistant", "content": final_text})
            save_history(self.history)
            try:
                from core import memory_reflect
                memory_reflect.maybe_reflect(
                    self.history, lambda m: self.client.chat(m, max_tokens=300, temperature=0.2))
            except Exception as _e:
                print(f"[memory] reflect hook error: {_e}")

    def resume_agent(self, transcript: list[dict], executed: list, user_message: str = "",
                     max_iters: int = 6) -> Generator[dict, None, None]:
        """
        Continue an agent loop that paused for confirmation. `executed` is a list
        of (call, result_dict) the caller ran after the user approved/denied them.
        Yields the same event stream as chat_agent.
        """
        from actions.agent import resume_agent as _resume, resume_agent_native
        from actions.tools import get_tools_openai_schema

        # Re-derive the tier the task used, so a coding task resumes on the coder.
        agent_max = int(os.getenv("KAMIL_AGENT_MAX_TOKENS", "6000"))
        agent_client = self.client
        agent_temp = 0.7
        agent_sampling = None
        if os.getenv("KAMIL_AGENT_HEAVY", "1") == "1" and self._looks_like_coding(user_message):
            try:
                from core import heavy_tier
                if heavy_tier.available() and heavy_tier.ensure_loaded():
                    from models.client import LlamaClient
                    agent_client = LlamaClient(base_url=heavy_tier.HEAVY_URL)
                    agent_temp = 0.25
                    agent_sampling = {"repeat_penalty": 1.1, "top_p": 0.9, "top_k": 40}
            except Exception as _e:
                print(f"[orchestrator] resume heavy tier unavailable: {_e}")

        if os.getenv("KAMIL_NATIVE_TOOLS", "1") == "1":
            tools_schema = get_tools_openai_schema()
            chat_fn = lambda msgs: agent_client.chat_tools(
                msgs, tools_schema, temperature=agent_temp, max_tokens=agent_max, sampling=agent_sampling)
            loop = resume_agent_native(chat_fn, transcript, executed, max_iters=max_iters,
                                       needs_confirm=_agent_needs_confirm())
        else:
            model_fn = lambda msgs: agent_client.chat(
                msgs, temperature=agent_temp, max_tokens=agent_max, sampling=agent_sampling)
            loop = _resume(model_fn, transcript, executed, max_iters=max_iters,
                           needs_confirm=_agent_needs_confirm())

        final_text = ""
        terminal = "done"
        for event in loop:
            _log_agent_event(event)
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

    def resolve_project(self, text: str) -> str | None:
        """The known project named in `text`, if any. Fuzzy: hyphens/underscores and
        spaces are interchangeable, so 'ai medical diagnosis' matches the project
        'AI-Medical-Diagnosis-System'. Longest exact match wins first; else a project
        whose distinctive words nearly all appear in the message."""
        import re
        if not self.indexer:
            return None
        def norm(s):
            return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", (s or "").lower())).strip()
        nt = " " + norm(text) + " "
        projects = self.indexer.list_projects()
        # 1) normalized project name appears verbatim in the message
        for p in sorted(projects, key=len, reverse=True):
            if " " + norm(p) + " " in nt or norm(p) in nt:
                return p
        # 2) fuzzy: >=2 distinctive project words present, >=75% of them
        _filler = {"system", "app", "demo", "project", "the", "and", "for", "ai"}
        best, best_score = None, 0.0
        for p in projects:
            toks = [t for t in norm(p).split() if len(t) > 2 and t not in _filler]
            if len(toks) < 2:
                continue
            hits = sum(1 for t in toks if f" {t} " in nt or f" {t}s " in nt)
            score = hits / len(toks)
            if hits >= 2 and score >= 0.75 and score > best_score:
                best, best_score = p, score
        return best

    def open_project(self, project_name: str) -> bool:
        """Load a project into context: scan it, make it current, wire the prompt
        block. This is the single path — auto-detect, the open_project tool, and
        the endpoint all go through here."""
        if not self.indexer:
            return False
        from core import activity
        with activity.task("opening project", project_name):
            self.indexer.scan_project_structure(project_name)
            self.indexer.current_project = project_name
            self.set_project(project_name, self.indexer)
        return True

    def set_project(self, project_name: str, indexer):
        self.enable_context(indexer)
        self.long_term._external_embedder = indexer.embedder
        project_path = os.path.join(PROJECTS_ROOT, project_name)
        # Confine agent writes/shell to this project's directory (kills the
        # "guessed a path / fell back to scratch" failure mode).
        try:
            from actions import tools as _tools
            _tools.set_active_project_dir(project_path)
        except Exception:
            pass
        self._project_block = f"""

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