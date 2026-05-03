from models.client import LlamaClient
from core.config import KAMIL_SYSTEM_PROMPT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS
from actions.dispatcher import parse_actions, requires_confirmation, execute_action
from memory.store import save_history, load_history, clear_history, save_setting, load_setting
from typing import Generator

class Orchestrator:
    def __init__(self):
        self.client = LlamaClient()
        self.history = load_history()
        self.system_prompt = KAMIL_SYSTEM_PROMPT
        self.indexer = None
        self.context_enabled = False
        self.council = None
        self._init_council()
        last_project = load_setting("last_project")
        if last_project:
            self._last_project = last_project
        else:
            self._last_project = None

    def _init_council(self):
        try:
            from core.council import Council
            self.council = Council()
        except Exception:
            self.council = None

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

    def enable_context(self, indexer):
        self.indexer = indexer
        self.context_enabled = True

    def _build_messages(self, user_message: str) -> list[dict]:
        system = self.system_prompt
        context_prefix = ""
    
        if self.context_enabled and self.indexer:
            results = self.indexer.query(user_message, n_results=3)
            if results:
                context_block = "\n\n".join([
                    f"[{r['metadata']['file_path'].split(chr(92))[-1]} | {r['metadata']['name']}]\n{r['text'][:200]}"
                    for r in results
                ])
                context_prefix = f"Retrieved context:\n\n{context_block}\n\nUser query: "

        # Keep only last 4 messages, estimate ~4 chars per token
        trimmed_history = self.history[-4:] if len(self.history) > 4 else self.history
    
        # Rough token estimate — if over 5000 tokens, trim to last 2 messages
        total_chars = len(system) + len(context_prefix) + sum(len(m["content"]) for m in trimmed_history)
        if total_chars > 20000:
            trimmed_history = self.history[-2:] if len(self.history) > 2 else self.history

        messages = [{"role": "system", "content": system}] + trimmed_history
        if context_prefix:
            messages.append({"role": "user", "content": context_prefix + user_message})
        else:
            messages.append({"role": "user", "content": user_message})
        return messages
    def chat_stream(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
        messages = self._build_messages(user_message)
        full_response = ""
        for chunk in self.client.chat_stream(messages, temperature=temperature, max_tokens=max_tokens):
            full_response += chunk
            yield chunk
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_response})
        save_history(self.history)

    def chat(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        messages = self._build_messages(user_message)
        response = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": response})
        save_history(self.history)
        return response

    def chat_with_actions(self, user_message: str) -> dict:
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

    def reset(self):
        self.history = []

    def set_project(self, project_name: str, indexer):
        self.enable_context(indexer)
        project_path = f"C:\\Users\\adnan\\Documents\\repos\\{project_name}"
        self.system_prompt = KAMIL_SYSTEM_PROMPT + f"""

Active project: {project_name}
Project directory: {project_path}
When writing files for this project, use paths inside: {project_path}
When the user says 'create a file' without specifying location, create it in the project directory."""

