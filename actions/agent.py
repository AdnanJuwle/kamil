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

import re
from typing import Callable, Iterator, Optional

from actions.tools import execute_tool, requires_confirmation, parse_tool_calls

DEFAULT_MAX_ITERS = 6
DEFAULT_MAX_RESULT_CHARS = 2000

_FENCE_RE = re.compile(r'```(?:tool|json)?\s*\n.*?```', re.DOTALL)


def strip_tool_blocks(text: str) -> str:
    """Remove fenced tool blocks and bare top-level JSON tool objects from prose."""
    text = _FENCE_RE.sub("", text)
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


def _format_results(results: list[tuple[dict, dict]], max_chars: int) -> str:
    """Render executed tool results into a compact message the model reads next."""
    lines = ["[tool results]"]
    for call, res in results:
        tool = call.get("tool", "?")
        if res.get("success"):
            out = res.get("output", res)
            text = str(out)
            if len(text) > max_chars:
                text = text[:max_chars] + f"... [truncated, {len(text)} chars total]"
            lines.append(f"{tool} -> OK: {text}")
        else:
            lines.append(f"{tool} -> ERROR: {res.get('error', 'unknown error')}")
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

        # If any requested tool needs approval, pause the whole step.
        pending = [c for c in calls if needs_confirm(c["tool"])]
        if pending:
            yield {
                "type": "awaiting_confirmation",
                "pending": pending,
                "transcript": messages + [{"role": "assistant", "content": resp}],
            }
            return

        # Execute auto tools and feed results back.
        results: list[tuple[dict, dict]] = []
        for c in calls:
            yield {"type": "tool_call", "tool": c["tool"], "params": c["params"]}
            res = _as_dict(executor(c["tool"], c["params"]))
            yield {"type": "tool_result", "tool": c["tool"],
                   "success": bool(res.get("success")), "result": res}
            results.append((c, res))

        messages.append({"role": "assistant", "content": resp})
        messages.append({"role": "user", "content": _format_results(results, max_result_chars)})

    yield {"type": "max_iterations", "text": last_text}


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
