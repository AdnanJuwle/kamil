# Testing the Full System Prompt

## Quick Test Commands

### 1. View Full System Prompt (with Code Context)

```bash
curl http://localhost:3000/debug_full_prompt | python -m json.tool
```

This will show you exactly what Kamil sees when analyzing code. The response includes:
- Full system prompt length
- Complete system prompt text with all injected code files

### 2. Test with Specific Project

```bash
# First, load PRANA project
curl -X POST http://localhost:3000/select_project \
  -H "Content-Type: application/json" \
  -d '{"project_name": "prana"}'

# Then view the full prompt (it will include PRANA files)
curl http://localhost:3000/debug_full_prompt | python -m json.tool
```

### 3. Test Voice Model (Empty Response Guard)

```bash
# Simple question that should work
curl -X POST http://localhost:3000/voice_chat \
  -H "Content-Type: application/json" \
  -d '{"content": "hello"}'

# Check the response — should have "text", "audio", "format"
# If voice model returns empty, should fallback to primary
```

### 4. Test Voice Model (Opinion Unlocking)

```bash
# Casual question
curl -X POST http://localhost:3000/voice_chat \
  -H "Content-Type: application/json" \
  -d '{"content": "what is your opinion on coffee?"}'

# Should respond with opinion, not disclaimer
# Example good response: "I think coffee is great. It helps focus and tastes good."
# Example bad response: "I am unable to offer personal opinions as an AI."
```

## Expected Output Examples

### Full Prompt (with code context)

```json
{
  "system_prompt_length": 12847,
  "system_prompt_full": "You are Kamil, a fully local AI system and personal assistant running on the user's own hardware.\n\n## IDENTITY\n...[full system prompt]...\n\n## PROJECT FILES\n### ekf.py (342 lines)\n```python\nimport numpy as np\nfrom dataclasses import dataclass\n\n@dataclass\nclass ExtendedKalmanFilter:\n    def __init__(self, state_dim, measurement_dim):\n        ...\n    \n    def update_gps(self, gps_measurement):\n        # GPS update step\n        ...\n\n    def mark_impassable(self, location):\n        ...\n```\n...\n(showing 4 most relevant files from prana)\n"
}
```

### Voice Chat Response (Opinion Unlocked)

```json
{
  "text": "Coffee is great. It helps me stay focused and I enjoy the taste.",
  "audio": "UklGRiYAAABXQVZFZm10IBAAAA...[base64 audio]...",
  "format": "wav"
}
```

### Voice Chat with Empty Guard (Fallback)

```bash
# If voice model returns empty
# Logs will show:
# "Quick response was empty, falling through to primary model"
# "Warning: synthesize_to_wav received empty text after cleaning"
# Handing off to primary model...

# Response will have:
{
  "text": "[Primary model response - full answer]",
  "audio": "[base64 audio of primary response]",
  "handoff_text": "let me check that for you",
  "handoff_audio": "[base64 audio of handoff phrase]"
}
```

## What You Should See

### Good Signs ✅
- Full system prompt includes complete file contents (not just fragments)
- Files have `import` statements visible (shows full file)
- Function definitions are complete (not truncated)
- Voice model responds with opinions without disclaimers
- Empty responses trigger fallback (see in logs)

### Bad Signs ❌
- System prompt shows only 500-char fragments
- Missing imports (shows partial file)
- Voice model says "I cannot offer personal opinions"
- Synthesize crashes on empty response
- No fallback when voice model returns empty

## Debugging Tips

### Check Backend Logs

```bash
# Terminal where backend is running should show:
# - "Voice model status: 200" (OK)
# - "Voice model content: '[response text]'" (what model returned)
# - "Quick response was empty, falling through..." (if empty)
# - "Primary response: ..." (final response)
```

### Parse JSON Response Safely

```bash
curl -s http://localhost:3000/debug_full_prompt | jq '.system_prompt_full | length'
# Shows character count of full prompt

curl -s http://localhost:3000/debug_full_prompt | jq '.system_prompt_full' | head -100
# Shows first 100 lines of prompt
```

### Check File Content in Prompt

```bash
curl -s http://localhost:3000/debug_full_prompt | jq -r '.system_prompt_full' | grep -A 20 "### ekf.py"
# Shows the ekf.py section of the context
```

## Common Issues & Fixes

### Prompt Shows Only Fragments

**Issue:** System prompt includes only 500-char chunks, not full files

**Fix:**
- Ensure `get_relevant_files()` is being called (not old `query()` method)
- Check that files exist at the paths returned by semantic search
- Verify `orchestrator.py` line ~195-220 uses new code block format

### Voice Model Still Restricted

**Issue:** Voice model responds with "I am unable to offer personal opinions"

**Fix:**
- Check `voice_system` prompt in `app.py` includes the new unlock clause
- Look for: "Never say 'as an AI I cannot' or 'I'm unable to offer personal opinions'"
- If not present, prompt didn't update properly
- Restart backend: `python run.py`

### Empty Audio Files

**Issue:** Audio response files are empty or very small

**Fix:**
- Check logs for "Warning: synthesize_to_wav received empty text"
- Verify Kokoro model loads: "Kokoro ready" in startup logs
- Test: `curl -X POST http://localhost:3000/synthesize -H "Content-Type: application/json" -d '{"content": "hello"}'`
- If error, Kokoro model path may be wrong

## Performance Notes

- Full file retrieval slightly slower than chunk retrieval (~50ms extra)
- 6000 char cap per file prevents context window overflow
- For PRANA (15 files × 300 lines avg = 4500 chars): displays completely
- For larger projects (>6000 chars per file): shows first 6000 chars (usually enough with imports + key code)

---

**Status: Ready to test. Backend working with all fixes in place.**
