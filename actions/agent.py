"""
actions/agent.py — Kamil's agentic tool-use loop.

The single thing that turns Kamil from "emits one action and stops" into "operates
the machine until the task is done": after each model turn, any AUTO tools are run
and their results are fed back into the conversation, so the model can read a file,
see its contents, then decide the next step — across multiple turns.

Design:
  - Pure and injectable. `iter_agent` takes a `model_fn(messages) -> str`, a tool
    `executor`, a `needs_confirm` predicate, and a `parse` function. Nothing here
    imports the LLM client, so the whole loop is unit-testable offline with a fake
    model that emits scripted tool calls.
  - Safety preserved: the moment a step requests a tool that needs approval
    (write_file / edit_file / shell), the loop PAUSES and yields
    `awaiting_confirmation` with a resumable transcript. Auto tools (reads,
    searches, listings, task creation) flow without interruption.

Events yielded (each a dict with "type"):
  text                  model prose for this step (tool blocks stripped)
  tool_call             {tool, params} about to run
  tool_result           {tool, success, result}
  awaiting_confirmation {pending:[calls], transcript:[messages]}  -> loop paused
  done                  {text}  -> model finished with a normal answer
  max_iterations        {text}  -> hit the iteration cap
  error                 {error}
"""

import os
import re
from typing import Callable, Iterator, Optional

from actions.tools import execute_tool, requires_confirmation, parse_tool_calls

DEFAULT_MAX_ITERS = 6
DEFAULT_MAX_RESULT_CHARS = 2000
# read_file result cap: big enough that most source files load in ONE read, so the
# model reviews from real content instead of paging with read_lines and running out
# of iterations — but bounded so a couple of reads don't overflow the coder's 32k
# context. ~32k chars ≈ 8k tokens ≈ 600 lines. (orchestrator.py at ~53KB truncates.)
READ_FILE_CAP = int(os.getenv("KAMIL_READ_FILE_CAP", "32000"))

_FENCE_RE = re.compile(r'```(?:tool|json)?\s*\n.*?```', re.DOTALL)
# Llama <function=...> tag syntax + <tool_call> wrappers, so they never leak as prose.
_FUNC_TAG_RE = re.compile(r'<function\s*=.*?(?:</function>|$)', re.DOTALL)
_TOOLCALL_RE = re.compile(r'</?tool_call>|</?parameter[^>]*>|</?function[^>]*>', re.IGNORECASE)


def strip_tool_blocks(text: str) -> str:
    """Remove fenced tool blocks, bare JSON tool objects, and Llama <function=...>
    tag syntax from prose (so malformed tool calls aren't spoken/shown)."""
    text = _FENCE_RE.sub("", text)
    text = _FUNC_TAG_RE.sub("", text)
    text = _TOOLCALL_RE.sub("", text)
    # remove bare balanced-brace objects that carry a "tool" key
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "{":
            depth, j = 0, i
            while j < n:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            chunk = text[i:j + 1]
            if '"tool"' in chunk:
                i = j + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out).strip()


def _as_dict(result) -> dict:
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


_TOOL_NAME_RE = (r"web_search|web_fetch|read_file|read_lines|list_dir|look|see|shell|"
                 r"write_file|edit_file|youtube|deep_research|open_project|list_projects")
# "let's call web_fetch", "I'll now search the web", "let me read the file", "next I will
# use the X tool" — an announced action with no actual tool call in the same turn.
_UNFIRED_TOOL_RE = re.compile(
    r"\b(let'?s|let me|i'?ll|i will|i'?m going to|going to|i should|next,? i|now i|"
    r"let me now)\b[^.!?\n]*\b(call|use|run|invoke|search|fetch|read|check|look)\b"
    r"[^.!?\n]*(" + _TOOL_NAME_RE + r"|\btool\b|\bweb\b)",
    re.IGNORECASE)


def _announces_unfired_tool(text: str) -> bool:
    """True if the prose announces an imminent tool call that wasn't actually emitted."""
    if not text or len(text) > 600:   # a real final answer is rarely a one-line announcement
        return False
    return bool(_UNFIRED_TOOL_RE.search(text))


def _format_results(results: list[tuple[dict, dict]], max_chars: int) -> str:
    """Render executed tool results into a message the model reads next. read_file
    is rendered as clean, fuller file content (the model must SEE a file to edit it)
    instead of a truncated dict repr."""
    lines = ["[tool results]"]
    for call, res in results:
        tool = call.get("tool", "?")
        if not res.get("success"):
            lines.append(f"{tool} -> ERROR: {res.get('error', 'unknown error')}")
            continue
        out = res.get("output", res)
        # read_file: show the real content (generously), not str(dict)
        if tool == "read_file" and isinstance(out, dict) and "content" in out:
            content = out["content"]
            cap = max(max_chars, READ_FILE_CAP)   # a whole source file in one read
            if len(content) > cap:
                content = (content[:cap] +
                           f"\n…[truncated at {cap} of {len(content)} chars — "
                           "read a specific region if you need more]")
            lines.append(f"read_file {out.get('path','')} ({out.get('lines','?')} lines):\n"
                         f"<<<\n{content}\n>>>")
        else:
            text = str(out)
            if len(text) > max_chars:
                text = text[:max_chars] + f"… [truncated, {len(text)} chars total]"
            lines.append(f"{tool} -> OK: {text}")
    lines.append(
        "\nUse these results. Call more tools if the task isn't finished, "
        "or give your final answer with NO tool block when you're done."
    )
    return "\n".join(lines)


def iter_agent(
    model_fn: Callable[[list[dict]], str],
    messages: list[dict],
    *,
    max_iters: int = DEFAULT_MAX_ITERS,
    executor: Callable = execute_tool,
    needs_confirm: Callable[[str], bool] = requires_confirmation,
    parse: Callable[[str], list[dict]] = parse_tool_calls,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> Iterator[dict]:
    """Run the tool-use loop, yielding events. See module docstring."""
    messages = list(messages)
    last_text = ""

    for _ in range(max_iters):
        try:
            resp = model_fn(messages)
        except Exception as e:
            yield {"type": "error", "error": f"model call failed: {e}"}
            return

        calls = parse(resp)
        text = strip_tool_blocks(resp)
        if text:
            last_text = text
            yield {"type": "text", "text": text}

        if not calls:
            yield {"type": "done", "text": text}
            return

        # needs_confirm may be tool-level (tool) or call-aware (tool, params);
        # adapt so an autonomous-mode predicate can auto-run in-fence mutations.
        def _confirm(c):
            try:
                return needs_confirm(c["tool"], c.get("params", {}))
            except TypeError:
                return needs_confirm(c["tool"])

        auto = [c for c in calls if not _confirm(c)]
        pending = [c for c in calls if _confirm(c)]

        # Run auto tools first so their work isn't lost if we then pause.
        results: list[tuple[dict, dict]] = []
        for c in auto:
            yield {"type": "tool_call", "tool": c["tool"], "params": c["params"]}
            res = _as_dict(executor(c["tool"], c["params"]))
            yield {"type": "tool_result", "tool": c["tool"],
                   "success": bool(res.get("success")), "result": res}
            results.append((c, res))

        # Any approval-gated tools pause the loop with a resumable transcript
        # that already includes the auto results from this step.
        if pending:
            paused = messages + [{"role": "assistant", "content": resp}]
            if results:
                paused = paused + [{"role": "user", "content": _format_results(results, max_result_chars)}]
            yield {"type": "awaiting_confirmation", "pending": pending, "transcript": paused}
            return

        messages.append({"role": "assistant", "content": resp})
        messages.append({"role": "user", "content": _format_results(results, max_result_chars)})

    yield {"type": "max_iterations", "text": last_text}


import json


def _native_calls(tool_calls: list) -> list[dict]:
    """Normalise OpenAI-style tool_calls to Kamil's {tool, params, _id}."""
    calls = []
    for tc in tool_calls or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw or "{}")
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"tool": fn.get("name"), "params": args, "_id": tc.get("id")})
    return calls


def _native_result_content(call: dict, res: dict, max_chars: int) -> str:
    """The content of a tool-role result message the model reads next."""
    if not res.get("success"):
        return f"ERROR: {res.get('error', 'unknown error')}"
    out = res.get("output", res)
    if call.get("tool") == "read_file" and isinstance(out, dict) and "content" in out:
        content = out["content"]
        cap = max(max_chars, READ_FILE_CAP)
        if len(content) > cap:
            content = content[:cap] + f"\n…[truncated at {cap} of {len(content)} chars]"
        return content
    text = str(out)
    return text if len(text) <= max_chars else text[:max_chars] + f"… [truncated, {len(text)} chars]"


def _assistant_turn(used_native: bool, content: str, native_calls: list) -> dict:
    if used_native:
        return {"role": "assistant", "content": content or None, "tool_calls": native_calls}
    return {"role": "assistant", "content": content}


def _thread_results(used_native: bool, results: list, max_chars: int) -> list[dict]:
    if used_native:
        return [{"role": "tool", "tool_call_id": c.get("_id"),
                 "content": _native_result_content(c, res, max_chars)} for c, res in results]
    return [{"role": "user", "content": _format_results(results, max_chars)}] if results else []


def iter_agent_native(
    chat_fn: Callable[[list[dict]], dict],
    messages: list[dict],
    *,
    max_iters: int = DEFAULT_MAX_ITERS,
    executor: Callable = execute_tool,
    needs_confirm: Callable = requires_confirmation,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    time_budget: float = None,
) -> Iterator[dict]:
    """Agent loop using NATIVE tool-calling. `chat_fn(messages)` returns an assistant
    message dict {"content", "tool_calls"}. Uses structured tool_calls when present,
    else falls back to parsing ```tool fences from the content — a strict superset of
    iter_agent. Same events; same confirm/pause semantics.

    `time_budget` (seconds) is a wall-clock ceiling on the whole loop: a general last
    resort so a turn can never run away for MINUTES (slow model, stuck tool, spiral)
    regardless of cause. Generous by default; background jobs don't count (they return
    immediately). Env KAMIL_AGENT_TIME_BUDGET overrides the default."""
    import time
    if time_budget is None:
        time_budget = float(os.getenv("KAMIL_AGENT_TIME_BUDGET", "240"))
    started = time.monotonic()
    messages = list(messages)
    last_text = ""
    nudged = False
    fail_streak = 0

    for _ in range(max_iters):
        # Wall-clock guard: stop before another (possibly slow) model call if we're over
        # budget. Return whatever we have rather than grinding on for minutes.
        if time_budget and (time.monotonic() - started) > time_budget:
            yield {"type": "max_iterations",
                   "text": last_text or "I spent too long on that without finishing — "
                   "tell me how you'd like me to narrow it down and I'll try again."}
            return
        try:
            msg = chat_fn(messages)
        except Exception as e:
            yield {"type": "error", "error": f"model call failed: {e}"}
            return

        content = msg.get("content") or ""
        native_calls = msg.get("tool_calls") or []
        used_native = bool(native_calls)
        calls = _native_calls(native_calls) if used_native else parse_tool_calls(content)

        text = content if used_native else strip_tool_blocks(content)
        if text and text.strip():
            last_text = text
            yield {"type": "text", "text": text}

        if not calls:
            # The weak model sometimes ANNOUNCES a tool call in prose ("let's call
            # web_fetch to read the first result") without actually emitting one, then
            # we'd treat that as the final answer and stall mid-task. Nudge it once to
            # actually fire the call instead of ending the turn.
            if not nudged and _announces_unfired_tool(text):
                nudged = True
                messages.append(_assistant_turn(used_native, content, native_calls))
                messages.append({"role": "user", "content":
                    "You described a tool call but didn't actually emit one. Emit the tool "
                    "call now as a real tool/function call — do not just say you will. If you "
                    "already have what you need, give the final answer instead."})
                continue
            yield {"type": "done", "text": text}
            return

        print(f"[agent] {'native' if used_native else 'text-fallback'} tool call: "
              + ", ".join(c.get("tool", "?") for c in calls))

        def _confirm(c):
            try:
                return needs_confirm(c["tool"], c.get("params", {}))
            except TypeError:
                return needs_confirm(c["tool"])

        auto = [c for c in calls if not _confirm(c)]
        pending = [c for c in calls if _confirm(c)]

        results: list[tuple[dict, dict]] = []
        for c in auto:
            yield {"type": "tool_call", "tool": c["tool"], "params": c["params"]}
            res = _as_dict(executor(c["tool"], c["params"]))
            yield {"type": "tool_result", "tool": c["tool"],
                   "success": bool(res.get("success")), "result": res}
            results.append((c, res))

        if pending:
            paused = messages + [_assistant_turn(used_native, content, native_calls)]
            paused += _thread_results(used_native, results, max_result_chars)
            yield {"type": "awaiting_confirmation", "pending": pending, "transcript": paused}
            return

        messages.append(_assistant_turn(used_native, content, native_calls))
        messages += _thread_results(used_native, results, max_result_chars)

        # Circuit-breaker: if the model keeps making tool calls that ALL fail (e.g. guessing
        # one fabricated URL/path after another), don't let it burn every iteration. After a
        # few all-failed rounds, tell it to stop guessing and answer; if it persists, end.
        if results and all(not r.get("success") for _, r in results):
            fail_streak += 1
        else:
            fail_streak = 0
        if fail_streak >= 3:
            messages.append({"role": "user", "content":
                "STOP: several tool calls in a row have failed. Do NOT call more tools — in "
                "particular do NOT guess more URLs or file paths. Answer now with what you "
                "already have, or tell the user plainly what you couldn't retrieve and why."})
            if fail_streak >= 4:
                yield {"type": "max_iterations", "text": last_text}
                return

    yield {"type": "max_iterations", "text": last_text}


def resume_agent_native(
    chat_fn: Callable[[list[dict]], dict],
    transcript: list[dict],
    executed: list[tuple[dict, dict]],
    **kwargs,
) -> Iterator[dict]:
    """Resume a native loop after confirmation: thread approved results as tool-role
    messages (matching the paused tool_calls) if the last assistant turn was native,
    else as a plain user message."""
    messages = list(transcript)
    native = any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages[-4:])
    if executed:
        messages += _thread_results(native, executed, kwargs.get("max_result_chars", DEFAULT_MAX_RESULT_CHARS))
    yield from iter_agent_native(chat_fn, messages, **kwargs)


def resume_agent(
    model_fn: Callable[[list[dict]], str],
    transcript: list[dict],
    executed: list[tuple[dict, dict]],
    **kwargs,
) -> Iterator[dict]:
    """
    Continue a loop that paused for confirmation. `executed` is the list of
    (call, result_dict) the caller ran after the user approved/denied them.
    """
    messages = list(transcript)
    if executed:
        messages.append({"role": "user", "content": _format_results(
            executed, kwargs.get("max_result_chars", DEFAULT_MAX_RESULT_CHARS))})
    yield from iter_agent(model_fn, messages, **kwargs)


def run_agent(model_fn, messages, **kwargs) -> dict:
    """Drain the loop and return a summary: terminal event + the event trail."""
    events = list(iter_agent(model_fn, messages, **kwargs))
    terminal = events[-1] if events else {"type": "error", "error": "no events"}
    return {
        "status": terminal["type"],
        "text": terminal.get("text", ""),
        "pending": terminal.get("pending", []),
        "transcript": terminal.get("transcript", []),
        "events": events,
    }
