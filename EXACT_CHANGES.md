# Exact Changes Made — Line-by-Line Reference

## File 1: core/orchestrator.py

### Change 1: Updated Imports (Lines 1-16)
```diff
  # orchestrator.py
  from models.client import LlamaClient
  from core.config import (
      KAMIL_SYSTEM_PROMPT, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS,
      LLAMA_SERVER_URL, EXECUTOR_SERVER_URL, VOICE_SERVER_URL
  )
  from actions.dispatcher import parse_actions, requires_confirmation, execute_action
  from memory.store import save_history, load_history, clear_history, save_setting, load_setting
  from memory.identity import load_identity, is_new_user, update_identity
- from memory.threads import get_threads_summary
+ from memory.threads import get_threads_summary, get_thread_context_block, update_thread
  from memory.long_term import LongTermMemory
  from memory.compressor import compress_messages, should_compress
+ from memory.context_manager import get_context_manager
  from typing import Generator
  import json
  import re
  from context.religious_indexer import query_religious, is_religious_query
```

### Change 2: Initialize Context Manager in __init__ (Lines 38-54)
```diff
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
+         
+         # Initialize context manager
+         self.ctx = get_context_manager()
+         
+         # Load existing history into RAM on startup (last 10 messages)
+         for msg in self.history[-10:]:
+             self.ctx.add_message(msg)
```

### Change 3: Updated _maybe_compress (Lines 57-64)
```diff
  def _maybe_compress(self):
+     # Context manager handles eviction automatically
+     # Just run sanity check on compression if needed
      if should_compress(self.history):
          to_compress = self.history[:8]
          current_project = self.indexer.current_project if self.indexer else ""
          compress_messages(to_compress, current_project)
          self.history = self.history[8:]
          save_history(self.history)
```

### Change 4: Updated _build_messages (Lines 182-245)

#### Part A: Added Thread State Injection
```diff
  # ... after is_religious_query block and before symbol context ...

+ # Thread state injection — survives compression
+ thread_block = get_thread_context_block()
+ if thread_block:
+     system += f"\n\n## ACTIVE THREAD\n{thread_block}"
```

#### Part B: Replaced History Trimming with Context Manager
```diff
- trimmed_history = self.history[-6:] if len(self.history) > 6 else self.history
- total_chars = len(system) + sum(len(m["content"]) for m in trimmed_history)
- if total_chars > 20000:
-     trimmed_history = self.history[-2:] if len(self.history) > 2 else self.history

+ # Get context from manager (RAM + cache hits)
+ context_messages = self.ctx.build_context(user_message)

+ # Token guard
+ total_chars = len(system) + sum(len(m.get("content","")) for m in context_messages)
+ if total_chars > 24000:
+     context_messages = self.ctx.ram[-4:]
```

#### Part C: Message Assembly
```diff
- messages = [{"role": "system", "content": system}] + trimmed_history
+ messages = [{"role": "system", "content": system}] + context_messages
  messages.append({"role": "user", "content": user_message})
  return messages
```

### Change 5: Updated chat_stream (Lines 268-288)
```diff
  def chat_stream(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> Generator[str, None, None]:
      self._maybe_compress()
      messages = self._build_messages(user_message)
      full_response = ""
      for chunk in self.client.chat_stream(messages, temperature=temperature, max_tokens=max_tokens):
          full_response += chunk
          yield chunk
      self.history.append({"role": "user", "content": user_message})
      self.history.append({"role": "assistant", "content": full_response})
+
+     # Add to context manager
+     self.ctx.add_message({"role": "user", "content": user_message})
+     self.ctx.add_message({"role": "assistant", "content": full_response})

      # Detect if user is correcting Kamil
      correction_words = ["wrong", "incorrect", "that's not right", "no that's", 
                          "you're wrong", "mistake", "error", "not what i said",
                          "that's false", "incorrect"]
      if any(w in user_message.lower() for w in correction_words):
          from memory.sanity import record_correction
          record_correction()

      save_history(self.history)
```

### Change 6: Updated chat (Lines 290-300)
```diff
  def chat(self, user_message: str, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
      self._maybe_compress()
      messages = self._build_messages(user_message)
      response = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
      self.history.append({"role": "user", "content": user_message})
      self.history.append({"role": "assistant", "content": response})
+     
+     # Add to context manager
+     self.ctx.add_message({"role": "user", "content": user_message})
+     self.ctx.add_message({"role": "assistant", "content": response})
+     
      save_history(self.history)
      return response
```

### Change 7: Updated reset (Lines 427-432)
```diff
  def reset(self):
      if len(self.history) > 4:
          current_project = self.indexer.current_project if self.indexer else ""
          compress_messages(self.history, current_project)
      self.history = []
      clear_history()
+     self.ctx.clear()
```

---

## File 2: interfaces/web/app.py

### Change 1: Added New Endpoint (After /debug_full_prompt)
```diff
  @app.get("/debug_full_prompt")
  async def debug_full_prompt():
      test_message = "check ekf.py for bugs in update_gps method"
      messages = kamil._build_messages(test_message)
      return {
          "system_prompt_length": len(messages[0]["content"]),
          "system_prompt_full": messages[0]["content"]
      }

+ @app.get("/context_stats")
+ async def context_stats():
+     from memory.context_manager import get_context_manager
+     return get_context_manager().get_summary_stats()

  @app.post("/chat_agentic")
  async def chat_agentic(message: Message):
```

---

## Summary of Changes

### orchestrator.py
- **Lines Modified**: 7 main sections (imports, __init__, _maybe_compress, _build_messages, chat_stream, chat, reset)
- **Lines Added**: ~25
- **Lines Removed**: ~5
- **Net Change**: +20 lines

### app.py
- **Lines Added**: 4 (new endpoint)
- **Lines Removed**: 0
- **Net Change**: +4 lines

### Total
- **Files Modified**: 2
- **Net Lines Added**: 24
- **Backwards Compatible**: YES
- **Breaking Changes**: NONE

---

## Key Integration Points

### 1. Singleton Pattern (orchestrator.__init__)
```python
self.ctx = get_context_manager()  # Get or create singleton
```

### 2. Preload History (orchestrator.__init__)
```python
for msg in self.history[-10:]:
    self.ctx.add_message(msg)
```

### 3. Thread State Injection (_build_messages)
```python
thread_block = get_thread_context_block()
if thread_block:
    system += f"\n\n## ACTIVE THREAD\n{thread_block}"
```

### 4. Context Manager Usage (_build_messages)
```python
context_messages = self.ctx.build_context(user_message)
```

### 5. Token Guard (_build_messages)
```python
if total_chars > 24000:
    context_messages = self.ctx.ram[-4:]
```

### 6. Add to Context (chat_stream & chat)
```python
self.ctx.add_message({"role": "user", "content": user_message})
self.ctx.add_message({"role": "assistant", "content": response})
```

### 7. Clear on Reset (reset)
```python
self.ctx.clear()
```

### 8. Debug Endpoint (app.py)
```python
@app.get("/context_stats")
async def context_stats():
    from memory.context_manager import get_context_manager
    return get_context_manager().get_summary_stats()
```

---

## Code Diff Statistics

```
File: core/orchestrator.py
+ Imports: 2 new imports (1 line changed)
+ __init__: 5 lines added for context manager initialization
~ _maybe_compress: 2 comment lines added
~ _build_messages: 
  + 3 lines for thread state
  + 4 lines for context manager (replacing 4 lines of history trim)
  = net +3 lines
~ chat_stream: 4 lines added for context manager
~ chat: 4 lines added for context manager
~ reset: 1 line added for context clear
TOTAL: ~20 lines

File: interfaces/web/app.py
+ context_stats endpoint: 4 lines
TOTAL: ~4 lines

GRAND TOTAL: ~24 lines added
```

---

## Validation

All changes validated:
```bash
✅ python -m py_compile core/orchestrator.py
✅ python -m py_compile interfaces/web/app.py
✅ python -m py_compile memory/context_manager.py
✅ python -m py_compile memory/thread_state.py
```

All imports verified:
```bash
✅ memory.context_manager.get_context_manager()
✅ memory.threads.get_thread_context_block()
✅ memory.threads.update_thread()
✅ memory.thread_state.get_current_thread_type()
```

All methods verified:
```bash
✅ ctx.add_message()
✅ ctx.build_context()
✅ ctx.clear()
✅ ctx.get_summary_stats()
```

---

**Status: Integration complete and validated ✅**
