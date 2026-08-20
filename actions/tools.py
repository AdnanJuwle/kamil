"""
Typed tool interface for Kamil.
Each tool has a name, schema, permission level, and execute function.
Permission levels:
  - auto: executes without asking (safe reads)
  - confirm: requires user approval (writes, shell)
  - deny: never executes
"""

import os
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Optional
from enum import Enum


class Permission(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


# ── Guardrails ─────────────────────────────────────────────────────
# Reads are broad but SECRETS are blocked (see _read_denied). WRITES and SHELL are
# fenced to these roots so an agent loop can't clobber system files or Kamil's own
# code. Note: C:\kamil (Kamil's own source) is deliberately NOT a write root — the
# agent can read its code to reason about itself but can never modify its own brain.
# When a project is loaded, writes are further confined to that project's directory.
# Override with KAMIL_WRITE_ROOTS (semicolon-separated absolute paths).
from core.config import PROJECTS_ROOT as _PROJECTS_ROOT
_DEFAULT_WRITE_ROOTS = _PROJECTS_ROOT

# When a project is open, writes/shell are confined to its directory (set by the
# orchestrator via set_active_project_dir). None = no project loaded.
_ACTIVE_PROJECT_DIR: Optional[Path] = None


def set_active_project_dir(path: Optional[str]) -> None:
    """Confine writes/shell to `path` (the loaded project). None clears the fence."""
    global _ACTIVE_PROJECT_DIR
    try:
        _ACTIVE_PROJECT_DIR = Path(path).resolve() if path else None
    except Exception:
        _ACTIVE_PROJECT_DIR = None


def active_project_dir() -> Optional[Path]:
    return _ACTIVE_PROJECT_DIR


import re as _re_secret
# Secrets Kamil must never READ — its own keys, .env, and the sealed code13/
# succession failsafe. This is the privacy guarantee in code: even Kamil can't
# exfiltrate its own secrets or the sealed word.
_SECRET_NAME_RE = _re_secret.compile(
    r'(^\.env(\.|$)|\.(key|pem|pfx|p12|keystore)$|^id_rsa|^credentials(\.|$)'
    r'|^secrets?\.(json|ya?ml|txt|env|ini)$)', _re_secret.I)


def _read_denied(path: str) -> tuple[bool, str]:
    """True if `path` is a secret off-limits to reads: the sealed code13/ dir, an
    .env, a private key, or a credentials file — anywhere on disk."""
    try:
        low = str(Path(path).resolve()).replace("/", "\\").lower()
    except Exception:
        low = str(path).replace("/", "\\").lower()
    if "\\code13\\" in low or low.endswith("\\code13"):
        return True, "code13/ is sealed and off-limits"
    if _SECRET_NAME_RE.search(os.path.basename(low)):
        return True, f"'{os.path.basename(str(path))}' is a secret (key/.env/credentials)"
    return False, ""


def _shell_touches_secret(command: str) -> bool:
    """A shell command that would read a secret (cat/type of code13, .env, keys)."""
    low = command.lower().replace("/", "\\")
    if "code13" in low:
        return True
    return bool(_re_secret.search(r'(\.env(\s|$|\.|")|\.(key|pem|pfx|p12)\b|\bid_rsa\b|\bcredentials\b)', low))

# First-token commands that are never run, plus dangerous substrings.
_BLOCKED_COMMANDS = {
    "rm", "rmdir", "del", "rd", "format", "shutdown", "reboot",
    "mkfs", "dd", "fdisk", "diskpart", "deltree", "erase",
}
_BLOCKED_SUBSTRINGS = (
    "rm -rf", "rm -r", "del /", "rmdir /s", "format ", ":\\ /q",
    "> /dev/", "shutdown", "mkfs", "fdisk",
)


def _write_roots() -> list[Path]:
    raw = os.getenv("KAMIL_WRITE_ROOTS", _DEFAULT_WRITE_ROOTS)
    roots = []
    for r in raw.split(";"):
        r = r.strip()
        if not r:
            continue
        try:
            roots.append(Path(r).resolve())
        except Exception:
            pass
    return roots


def _within_write_roots(path: str) -> bool:
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    # A loaded project confines writes to its own directory — nothing else,
    # so the agent can't fall back to a scratch dir when it loses the target.
    if _ACTIVE_PROJECT_DIR is not None:
        try:
            p.relative_to(_ACTIVE_PROJECT_DIR)
            return True
        except ValueError:
            return False
    for root in _write_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _outside_roots_msg(path: str) -> str:
    """Refusal that steers the agent to open_project instead of guessing/falling back."""
    try:
        low = str(Path(path).resolve()).replace("/", "\\").lower()
    except Exception:
        low = str(path).replace("/", "\\").lower()
    if low.startswith("c:\\kamil\\") or low == "c:\\kamil":
        return ("refused: that is KAMIL'S OWN SOURCE CODE — it is strictly read-only. You can "
                "read and critique your own code but you cannot modify it; changes to your own "
                "brain go through Adnan. Deliver your improvements as TEXT suggestions, not edits.")
    if _ACTIVE_PROJECT_DIR is not None:
        return (f"refused: '{path}' is outside the loaded project ({_ACTIVE_PROJECT_DIR}). "
                "Write inside the project directory. If you meant a different project, call "
                "open_project first — do NOT invent a path or write to a scratch folder.")
    return (f"refused: '{path}' is outside the allowed write roots and no project is loaded. "
            "Call list_projects then open_project to load the right project FIRST, then write "
            "with a full path inside it. Never guess a path or fall back to a scratch folder.")


def _command_allowed(command: str) -> tuple[bool, str]:
    stripped = command.strip()
    if not stripped:
        return False, "empty command"
    first = stripped.split()[0].lower().lstrip('"\'').split("\\")[-1].split("/")[-1]
    if first in _BLOCKED_COMMANDS:
        return False, f"command '{first}' is blocked for safety"
    low = stripped.lower()
    for bad in _BLOCKED_SUBSTRINGS:
        if bad in low:
            return False, f"command contains blocked pattern '{bad.strip()}'"
    return True, ""


def _is_script_launch(command: str) -> bool:
    """A bare `python file.py` style launch — which hangs the agent on GUI apps or
    servers (no display). py_compile / pytest / -c / --version are fine."""
    import re as _re
    low = command.strip().lower()
    if not (_re.match(r'^(py|python|python3)\b', low) and _re.search(r'\.py(\s|"|$)', low)):
        return False
    return not _re.search(r'(-m\s+py_compile|py_compile|-m\s+pytest|pytest|-c\s|--version|-m\s+pip)', low)


def _is_long_running(command: str) -> bool:
    """Commands that routinely take minutes (installs, builds, downloads) and would
    otherwise die at the shell timeout. Run these in the background automatically."""
    import re as _re
    low = command.strip().lower()
    return bool(_re.search(
        r'\b(pip\s+install|pip3\s+install|python\s+-m\s+pip\s+install|'
        r'npm\s+(install|ci|run\s+build|run\s+dev)|npx\b|yarn\b|pnpm\b|'
        r'flutter\s+(pub\s+get|build|run)|gradle\b|\.\/gradlew|gradlew|'
        r'cargo\s+(build|install)|go\s+(build|install|get)|make\b|cmake\s+--build|'
        r'docker\s+(build|pull)|git\s+clone|conda\s+install|apt(-get)?\s+install|'
        r'huggingface-cli\s+download|wget\b|curl\s+-[^|]*\bO\b)', low))


def _job_status(params: dict) -> "ToolResult":
    """Check on a background shell job started earlier."""
    from core import bg_jobs
    job_id = params.get("job_id", "")
    st = bg_jobs.status(job_id)
    if st is None:
        return ToolResult(success=False, tool="job_status", params=params,
                          error=f"no background job named '{job_id}'. Running jobs: "
                                + (", ".join(j["job_id"] for j in bg_jobs.list_jobs()) or "none"))
    tail = st.get("output", "")
    if len(tail) > 3000:
        tail = "…" + tail[-3000:]
    return ToolResult(success=True, tool="job_status", params=params,
                      output={"job_id": st["job_id"], "status": st["status"],
                              "returncode": st["returncode"], "label": st["label"],
                              "output_tail": tail})


_VISION_SYS = ("You are Kamil's eyes. Describe only what is actually visible, concretely and "
               "concisely. Read any visible text verbatim. Never speculate about what you cannot see.")


def _vision_answer(data: bytes, question: str, source: str, mime: str = "image/jpeg"):
    """Shared path for see/look: publish the live preview, run the VLM, publish the
    result. Returns (answer, error) — exactly one is non-None."""
    from core import vision_tier, vision_live
    vision_live.set_seeing(vision_live.make_thumb(data, mime), question, source)
    if not vision_tier.ensure_loaded():
        vision_live.set_result("(vision model failed to load)")
        return None, "vision model failed to load in time"
    from models.client import LlamaClient
    client = LlamaClient(base_url=vision_tier.VISION_URL)
    try:
        answer = client.describe_image(data, prompt=question, mime=mime, system=_VISION_SYS)
        vision_tier.mark_activity()
        vision_live.set_result(answer)
        return answer, None
    except Exception as e:
        vision_live.set_result("(error reading image)")
        return None, str(e)


def _see(params: dict) -> "ToolResult":
    """Look at an image file and answer a question about it, via the local vision tier."""
    path = params.get("image_path") or params.get("path") or ""
    question = (params.get("question") or
                "What is in this image? Describe it concisely and read any visible text.")
    if not os.path.isfile(path):
        return ToolResult(success=False, tool="see", params=params, error=f"image not found: {path}")
    from core import vision_tier
    if not vision_tier.available():
        return ToolResult(success=False, tool="see", params=params,
                          error=f"vision unavailable: {vision_tier.missing_reason()}")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        return ToolResult(success=False, tool="see", params=params, error=f"couldn't read image: {e}")
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    answer, err = _vision_answer(data, question, os.path.basename(path), mime)
    if err:
        return ToolResult(success=False, tool="see", params=params, error=err)
    return ToolResult(success=True, tool="see", params=params,
                      output={"answer": answer, "image": os.path.basename(path)})


def _capture_webcam(camera: int = 0):
    """Grab one frame from a local webcam as JPEG bytes. (answer, error)."""
    try:
        import cv2
    except Exception:
        return None, "webcam capture needs opencv (pip install opencv-python)"
    cap = None
    try:
        cap = cv2.VideoCapture(camera, cv2.CAP_DSHOW)   # DSHOW is fast/reliable on Windows
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(camera)              # fall back to default backend
        ok, frame = False, None
        if cap.isOpened():
            for _ in range(6):                          # discard warm-up frames (auto-exposure)
                ok, frame = cap.read()
        if not ok or frame is None:
            return None, f"couldn't read from webcam {camera} (is it connected / not in use?)"
        okj, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not okj:
            return None, "failed to encode webcam frame"
        return buf.tobytes(), None
    except Exception as e:
        return None, str(e)
    finally:
        if cap is not None:
            cap.release()


def _look(params: dict) -> "ToolResult":
    """Capture the current webcam frame and answer a question about it — Kamil looking
    with his own eyes. Use when Adnan asks what's in front of him / on the desk / etc."""
    question = (params.get("question") or
                "What am I looking at? Describe it concisely and read any visible text.")
    camera = int(params.get("camera", 0))
    from core import vision_tier
    if not vision_tier.available():
        return ToolResult(success=False, tool="look", params=params,
                          error=f"vision unavailable: {vision_tier.missing_reason()}")
    data, err = _capture_webcam(camera)
    if err:
        return ToolResult(success=False, tool="look", params=params, error=err)
    answer, err = _vision_answer(data, question, "webcam", "image/jpeg")
    if err:
        return ToolResult(success=False, tool="look", params=params, error=err)
    return ToolResult(success=True, tool="look", params=params, output={"answer": answer})


def _learn_skill(params: dict) -> "ToolResult":
    """Save a reusable procedure (a 'skill') so Kamil can follow it next time."""
    from core import skill_store
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, tool="learn_skill", params=params, error="need a skill name")
    if not (params.get("procedure") or "").strip():
        return ToolResult(success=False, tool="learn_skill", params=params, error="need the procedure steps")
    r = skill_store.save_skill(name, params.get("when_to_use", ""), params.get("procedure", ""),
                               params.get("pitfalls", ""), params.get("verification", ""),
                               params.get("tags"), source="taught")
    return ToolResult(success=True, tool="learn_skill", params=params,
                      output={"learned": r["name"], "slug": r["slug"]})


def _is_shell_file_write(command: str) -> bool:
    """A shell command that writes/edits a file — heredoc, tee, sed -i, PowerShell
    writers, or a `> file` redirect. Files must go through write_file/edit_file (which
    show a diff and honour the write roots), not raw shell. (Odysseus enforces the
    same: bash is read-only inspection.)"""
    import re as _re
    low = command.strip().lower()
    if _re.search(r'<<-?\s*[\'"]?\w', low):                       # heredoc
        return True
    if _re.search(r'\btee\b|\bsed\s+-i\b|\bawk\b[^|]*-i\b', low): # tee / in-place edit
        return True
    if _re.search(r'\b(out-file|set-content|add-content)\b', low):  # PowerShell writers
        return True
    # output redirection to a file, ignoring 2>&1 / >/dev/null / >nul
    stripped = _re.sub(r'2>&1|2?>\s*(?:/dev/null|nul)\b', '', low)
    if _re.search(r'>>?\s*[\'"]?[\w./\\~-]+', stripped):
        return True
    return False


@dataclass
class ToolParam:
    name: str
    type: str  # "string" | "int" | "bool" | "list"
    required: bool = True
    description: str = ""
    default: Any = None


@dataclass 
class Tool:
    name: str
    description: str
    params: list[ToolParam]
    permission: Permission
    execute: Callable


@dataclass
class ToolResult:
    success: bool
    output: Any = None
    error: str = ""
    tool: str = ""
    params: dict = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "tool": self.tool,
            "params": self.params or {}
        }


# ── Tool implementations ──────────────────────────────────────────

def _read_file(params: dict) -> ToolResult:
    path = params["path"]
    denied, why = _read_denied(path)
    if denied:
        return ToolResult(success=False, tool="read_file", params=params,
                          error=f"refused: {why}. Kamil doesn't read secrets — not even its own.")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.splitlines()
        return ToolResult(
            success=True,
            output={"content": content, "lines": len(lines), "path": path},
            tool="read_file",
            params=params
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="read_file", params=params)


def _clean_written_content(content) -> str:
    """Small models sometimes wrap file content in a markdown code fence and/or
    double-escape newlines, so the file ends up with a literal ```python line and
    literal '\\n' instead of real line breaks. Strip a wrapping fence and unescape
    when the content has escaped newlines but no real ones."""
    if not isinstance(content, str):
        return content
    import re
    c = content
    # 1) unescape double-escaped whitespace first (literal \n but no real newline)
    if ('\\n' in c or '\\t' in c) and '\n' not in c:
        c = (c.replace('\\r\\n', '\n').replace('\\n', '\n')
               .replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\'))
    # 2) then strip a wrapping ```lang … ``` fence if the whole content is one block
    m = re.match(r'^\s*```[^\n`]*\n(.*?)\n?```\s*$', c, re.S)
    if m:
        c = m.group(1)
    return c


def _write_file(params: dict) -> ToolResult:
    path = params["path"]
    content = _clean_written_content(params["content"])
    if not _within_write_roots(path):
        return ToolResult(
            success=False, tool="write_file", params=params,
            error=_outside_roots_msg(path))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(
            success=True,
            output={"path": path, "bytes": len(content.encode())},
            tool="write_file",
            params=params
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="write_file", params=params)


def _edit_file(params: dict) -> ToolResult:
    """Surgical, unambiguous replace — the reliable way to change code."""
    path = params["path"]
    old = params["old"]
    new = _clean_written_content(params["new"])
    if not _within_write_roots(path):
        return ToolResult(
            success=False,
            error=_outside_roots_msg(path),
            tool="edit_file", params=params
        )
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="edit_file", params=params)
    occurrences = original.count(old)
    if occurrences == 0:
        return ToolResult(success=False, error="target text not found — no change made",
                          tool="edit_file", params=params)
    if occurrences > 1:
        return ToolResult(success=False,
                          error=f"target text appears {occurrences} times — too ambiguous; include more surrounding context",
                          tool="edit_file", params=params)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(original.replace(old, new, 1))
        return ToolResult(success=True, output={"path": path, "replaced": 1},
                          tool="edit_file", params=params)
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="edit_file", params=params)


def _list_directory(params: dict) -> ToolResult:
    path = params["path"]
    denied, why = _read_denied(path)
    if denied:
        return ToolResult(success=False, tool="list_directory", params=params,
                          error=f"refused: {why}.")
    try:
        entries = []
        for entry in os.scandir(path):
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None
            })
        entries.sort(key=lambda x: (x["type"] == "file", x["name"]))
        return ToolResult(
            success=True,
            output={"path": path, "entries": entries, "count": len(entries)},
            tool="list_directory",
            params=params
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="list_directory", params=params)


def _shell(params: dict) -> ToolResult:
    command = params["command"]
    cwd = params.get("cwd", "C:\\kamil")
    timeout = params.get("timeout", 30)
    allowed, reason = _command_allowed(command)
    if not allowed:
        return ToolResult(success=False, error=f"refused: {reason}", tool="shell", params=params)
    if _shell_touches_secret(command):
        return ToolResult(success=False, tool="shell", params=params,
                          error="refused: that command reads a secret (code13/, .env, or a key). Off-limits.")
    if _is_script_launch(command):
        return ToolResult(success=False, tool="shell", params=params,
                          error=("won't run a script directly — GUI apps and servers block with no "
                                 "display and hang. Verify it with `python -m py_compile <path>` instead, "
                                 "then tell the user how to run it themselves. You're done once it compiles."))
    if _is_shell_file_write(command):
        return ToolResult(success=False, tool="shell", params=params,
                          error=("don't write files with shell (redirects/heredocs/tee/sed -i). Use the "
                                 "write_file tool to create a file or edit_file to change one — they honour "
                                 "the write roots and show a diff. shell is for read-only inspection "
                                 "(ls, cat, grep, git status) and mkdir."))
    if params.get("background") or _is_long_running(command):
        from core import bg_jobs
        job = bg_jobs.start(command, cwd)
        return ToolResult(
            success=True, tool="shell", params=params,
            output={"background": True, "job_id": job["job_id"], "label": job["label"],
                    "note": (f"started in background as {job['job_id']} — it keeps running past the "
                             "shell timeout and shows in the Work tab. Don't wait on it; use the "
                             "job_status tool with this job_id if you need the result later.")})
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout
        )
        return ToolResult(
            success=result.returncode == 0,
            output={
                "stdout": result.stdout[:3000],
                "stderr": result.stderr[:1000],
                "returncode": result.returncode
            },
            tool="shell",
            params=params
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Command timed out", tool="shell", params=params)
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="shell", params=params)


def _search_files(params: dict) -> ToolResult:
    directory = params["directory"]
    pattern = params["pattern"]
    try:
        import glob
        matches = glob.glob(os.path.join(directory, "**", pattern), recursive=True)
        matches = matches[:50]  # cap at 50
        return ToolResult(
            success=True,
            output={"matches": matches, "count": len(matches)},
            tool="search_files",
            params=params
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="search_files", params=params)


def _create_task(params: dict) -> ToolResult:
    try:
        from actions.tasks import create_task
        task = create_task(
            title=params["title"],
            description=params.get("description", ""),
            project=params.get("project", "")
        )
        return ToolResult(
            success=True,
            output={"task": task},
            tool="create_task",
            params=params
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="create_task", params=params)


def _remind(params: dict) -> ToolResult:
    """Set a reminder Kamil will surface when due. Give 'at' (ISO time) or 'in_minutes'."""
    from datetime import datetime, timedelta
    text = params.get("text", "").strip()
    if not text:
        return ToolResult(success=False, error="reminder text is required", tool="remind", params=params)
    try:
        if params.get("at"):
            due = datetime.fromisoformat(params["at"])
        elif params.get("in_minutes") is not None:
            due = datetime.now() + timedelta(minutes=int(params["in_minutes"]))
        else:
            return ToolResult(success=False, error="provide 'at' (ISO 8601) or 'in_minutes'",
                              tool="remind", params=params)
    except Exception as e:
        return ToolResult(success=False, error=f"bad time: {e}", tool="remind", params=params)
    from core.reminders import add_reminder
    r = add_reminder(text, due)
    return ToolResult(success=True, output={"id": r["id"], "text": r["text"], "due": r["due"]},
                      tool="remind", params=params)


def _remember(params: dict) -> ToolResult:
    """Append a durable fact to Kamil's own memory notes (memory.md)."""
    from core.self_memory import append_note
    r = append_note(params.get("note", ""))
    if not r.get("success"):
        return ToolResult(success=False, error=r.get("error", "failed"), tool="remember", params=params)
    return ToolResult(success=True, output=r, tool="remember", params=params)


def _update_memory(params: dict) -> ToolResult:
    """Replace the whole memory-notes body with a curated/consolidated version."""
    from core.self_memory import set_memory
    r = set_memory(params.get("content", ""))
    return ToolResult(success=True, output=r, tool="update_memory", params=params)


def _forget(params: dict) -> ToolResult:
    """Surgically remove only the memory note(s) matching a query."""
    from core.self_memory import forget
    r = forget(params.get("query", ""))
    if not r.get("success"):
        return ToolResult(success=False, error=r.get("error", "failed"), tool="forget", params=params)
    return ToolResult(success=True, output=r, tool="forget", params=params)


def _grep(params: dict) -> ToolResult:
    """Search file CONTENTS (regex) across a directory — find code by what it contains."""
    import re as _re
    import fnmatch
    pattern = params.get("pattern", "")
    directory = params.get("directory", ".")
    glob_pat = params.get("glob", "*")
    max_results = int(params.get("max_results", 60))
    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return ToolResult(success=False, error=f"bad regex: {e}", tool="grep", params=params)
    _SKIP = {".git", "node_modules", "__pycache__", "venv", "venv313", ".venv", "chroma"}
    matches = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for fn in files:
            if not fnmatch.fnmatch(fn, glob_pat):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            matches.append({"file": path, "line": i, "text": line.strip()[:200]})
                            if len(matches) >= max_results:
                                return ToolResult(success=True, tool="grep", params=params,
                                                  output={"matches": matches, "truncated": True})
            except (OSError, UnicodeError):
                continue
    return ToolResult(success=True, output={"matches": matches, "count": len(matches)},
                      tool="grep", params=params)


def _read_lines(params: dict) -> ToolResult:
    """Read a specific line range of a file (for big files you can't read whole)."""
    path = params["path"]
    start = max(1, int(params.get("start", 1)))
    end = int(params.get("end", start + 200))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return ToolResult(success=False, error=str(e), tool="read_lines", params=params)
    chunk = lines[start - 1:end]
    numbered = "".join(f"{start + i}\t{ln}" for i, ln in enumerate(chunk))
    return ToolResult(success=True, tool="read_lines", params=params,
                      output={"path": path, "start": start, "end": min(end, len(lines)),
                              "total_lines": len(lines), "content": numbered})


def _web_search(params: dict) -> ToolResult:
    from actions.web import search_web
    res = search_web(params.get("query", ""), params.get("n", 5))
    if not res:
        return ToolResult(success=False, error="no results (or search unavailable)",
                          tool="web_search", params=params)
    return ToolResult(success=True, output={"results": res}, tool="web_search", params=params)


def _web_fetch(params: dict) -> ToolResult:
    from actions.web import fetch_url
    r = fetch_url(params.get("url", ""), params.get("max_chars", 6000))
    if not r.get("success"):
        return ToolResult(success=False, error=r.get("error", "fetch failed"),
                          tool="web_fetch", params=params)
    return ToolResult(success=True, output=r, tool="web_fetch", params=params)


def _learn(params: dict) -> ToolResult:
    """Ingest a document into Kamil's skill library so it can be retrieved later."""
    path = params.get("path", "")
    if not os.path.isfile(path):
        return ToolResult(success=False, error=f"file not found: {path}", tool="learn", params=params)
    from context.domain_indexer import learn_document
    r = learn_document(path, domain=params.get("domain", "general"))
    if not r.get("success"):
        return ToolResult(success=False, error=r.get("error", "ingest failed"), tool="learn", params=params)
    return ToolResult(success=True, output=r, tool="learn", params=params)


def _list_projects(params: dict) -> ToolResult:
    """List the projects Kamil can open, and which one is loaded now."""
    from core.orchestrator import get_orchestrator
    orch = get_orchestrator()
    if not orch or not orch.indexer:
        return ToolResult(success=False, error="no project indexer available", tool="list_projects", params=params)
    return ToolResult(success=True, tool="list_projects", params=params, output={
        "projects": orch.indexer.list_projects(),
        "current": orch.indexer.current_project,
    })


def _open_project(params: dict) -> ToolResult:
    """Load a codebase into context so Kamil can read/reason about it. Accepts an
    exact or partial name; resolves against the available projects."""
    from core.orchestrator import get_orchestrator
    orch = get_orchestrator()
    if not orch or not orch.indexer:
        return ToolResult(success=False, error="no project indexer available", tool="open_project", params=params)
    name = (params.get("name", "") or "").strip()
    available = orch.indexer.list_projects()
    match = next((p for p in available if p.lower() == name.lower()), None) \
        or next((p for p in sorted(available, key=len, reverse=True) if name and name.lower() in p.lower()), None)
    if not match:
        return ToolResult(success=False, tool="open_project", params=params,
                          error=f"no project matching '{name}'. available: {', '.join(available)}")
    orch.open_project(match)
    return ToolResult(success=True, tool="open_project", params=params,
                      output={"opened": match, "current": match})


def _get_system_info(params: dict) -> ToolResult:
    import platform
    try:
        import psutil
        info = {
            "os": platform.system(),
            "cpu_percent": psutil.cpu_percent(interval=1),
            "ram_used_gb": round(psutil.virtual_memory().used / 1e9, 2),
            "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 2),
            "disk_free_gb": round(psutil.disk_usage("C:\\").free / 1e9, 2),
        }
    except ImportError:
        info = {"os": platform.system(), "note": "install psutil for full info"}
    return ToolResult(success=True, output=info, tool="system_info", params=params)


# ── Tool registry ─────────────────────────────────────────────────

TOOLS: dict[str, Tool] = {
    "read_file": Tool(
        name="read_file",
        description="Read the contents of a file",
        params=[
            ToolParam("path", "string", required=True, description="Absolute file path")
        ],
        permission=Permission.AUTO,
        execute=_read_file
    ),
    "write_file": Tool(
        name="write_file",
        description="Write content to a file, creating it if needed",
        params=[
            ToolParam("path", "string", required=True, description="Absolute file path"),
            ToolParam("content", "string", required=True, description="Content to write")
        ],
        permission=Permission.CONFIRM,
        execute=_write_file
    ),
    "edit_file": Tool(
        name="edit_file",
        description="Replace an exact, unique snippet in a file (surgical edit). Preferred over write_file for changing existing files.",
        params=[
            ToolParam("path", "string", required=True, description="Absolute file path"),
            ToolParam("old", "string", required=True, description="Exact existing text to replace (must be unique in the file)"),
            ToolParam("new", "string", required=True, description="Replacement text")
        ],
        permission=Permission.CONFIRM,
        execute=_edit_file
    ),
    "list_directory": Tool(
        name="list_directory",
        description="List files and directories at a path",
        params=[
            ToolParam("path", "string", required=True, description="Directory path")
        ],
        permission=Permission.AUTO,
        execute=_list_directory
    ),
    "shell": Tool(
        name="shell",
        description="Execute a shell command",
        params=[
            ToolParam("command", "string", required=True, description="Command to run"),
            ToolParam("cwd", "string", required=False, description="Working directory", default="C:\\kamil"),
            ToolParam("timeout", "int", required=False, description="Timeout in seconds", default=30),
            ToolParam("background", "bool", required=False, description=(
                "Run without waiting — for long commands (installs, builds, downloads) that "
                "would exceed the timeout. Returns a job_id; poll it with job_status."), default=False)
        ],
        permission=Permission.CONFIRM,
        execute=_shell
    ),
    "job_status": Tool(
        name="job_status",
        description="Check on a background shell job (started by shell with background=true)",
        params=[
            ToolParam("job_id", "string", required=True, description="The job id returned by shell")
        ],
        permission=Permission.AUTO,
        execute=_job_status
    ),
    "learn_skill": Tool(
        name="learn_skill",
        description="Save a reusable procedure (a 'skill') so you can follow it next time — "
                    "use after figuring out how to do a recurring task, or when Adnan teaches you one.",
        params=[
            ToolParam("name", "string", required=True, description="Short skill name"),
            ToolParam("when_to_use", "string", required=False, description="Trigger conditions / when this applies"),
            ToolParam("procedure", "string", required=True, description="The steps to follow (numbered or prose)"),
            ToolParam("pitfalls", "string", required=False, description="Common failure modes + recovery"),
            ToolParam("verification", "string", required=False, description="How to confirm success"),
            ToolParam("tags", "string", required=False, description="Comma-separated tags"),
        ],
        permission=Permission.AUTO,
        execute=_learn_skill
    ),
    "see": Tool(
        name="see",
        description="Look at an image file and answer a question about it (local vision model). "
                    "Use for photos, screenshots, camera frames — 'what is this', reading text/signs.",
        params=[
            ToolParam("image_path", "string", required=True, description="Path to the image file"),
            ToolParam("question", "string", required=False,
                      description="What to ask about the image", default="Describe this image.")
        ],
        permission=Permission.AUTO,
        execute=_see
    ),
    "look": Tool(
        name="look",
        description="Capture the current webcam frame and answer a question about it — Kamil "
                    "looking with his own eyes. Use when asked what's in front of him / on the "
                    "desk / 'what am I looking at' / 'what do you see'.",
        params=[
            ToolParam("question", "string", required=False,
                      description="What to ask about what the camera sees",
                      default="What am I looking at?"),
            ToolParam("camera", "int", required=False, description="Camera index", default=0)
        ],
        permission=Permission.AUTO,
        execute=_look
    ),
    "search_files": Tool(
        name="search_files",
        description="Search for files matching a pattern in a directory",
        params=[
            ToolParam("directory", "string", required=True, description="Directory to search"),
            ToolParam("pattern", "string", required=True, description="Glob pattern e.g. *.py")
        ],
        permission=Permission.AUTO,
        execute=_search_files
    ),
    "grep": Tool(
        name="grep",
        description="Search file CONTENTS by regex across a directory (find where code/text lives). Returns file:line matches.",
        params=[
            ToolParam("pattern", "string", required=True, description="Regex to search for"),
            ToolParam("directory", "string", required=False, description="Directory to search (default '.')"),
            ToolParam("glob", "string", required=False, description="Filename filter, e.g. *.py (default *)"),
            ToolParam("max_results", "int", required=False, description="Cap results (default 60)"),
        ],
        permission=Permission.AUTO,
        execute=_grep
    ),
    "read_lines": Tool(
        name="read_lines",
        description="Read a specific line range of a file (for big files) — returns numbered lines.",
        params=[
            ToolParam("path", "string", required=True, description="Absolute file path"),
            ToolParam("start", "int", required=False, description="First line (1-indexed, default 1)"),
            ToolParam("end", "int", required=False, description="Last line (default start+200)"),
        ],
        permission=Permission.AUTO,
        execute=_read_lines
    ),
    "web_search": Tool(
        name="web_search",
        description="Search the web for current information (news, docs, anything past your 2023 cutoff). Returns titles, urls, snippets.",
        params=[
            ToolParam("query", "string", required=True, description="Search query"),
            ToolParam("n", "int", required=False, description="Number of results (default 5)"),
        ],
        permission=Permission.AUTO,
        execute=_web_search
    ),
    "web_fetch": Tool(
        name="web_fetch",
        description="Fetch a web page and return its readable text — use after web_search to read a result.",
        params=[
            ToolParam("url", "string", required=True, description="The URL to read"),
            ToolParam("max_chars", "int", required=False, description="Max chars to return (default 6000)"),
        ],
        permission=Permission.AUTO,
        execute=_web_fetch
    ),
    "create_task": Tool(
        name="create_task",
        description="Create a new task",
        params=[
            ToolParam("title", "string", required=True, description="Task title"),
            ToolParam("description", "string", required=False, description="Task details", default=""),
            ToolParam("project", "string", required=False, description="Project name", default="")
        ],
        permission=Permission.AUTO,
        execute=_create_task
    ),
    "system_info": Tool(
        name="system_info",
        description="Get system resource usage",
        params=[],
        permission=Permission.AUTO,
        execute=_get_system_info
    ),
    "learn": Tool(
        name="learn",
        description="Ingest a document (pdf/epub/txt/md) into your skill library so you can retrieve its knowledge later",
        params=[
            ToolParam("path", "string", required=True, description="Absolute path to the document"),
            ToolParam("domain", "string", required=False, description="Skill category, e.g. 'robotics' (default: general)"),
        ],
        permission=Permission.AUTO,
        execute=_learn
    ),
    "remind": Tool(
        name="remind",
        description="Set a time-based reminder Kamil surfaces when due (give 'at' as ISO time, or 'in_minutes')",
        params=[
            ToolParam("text", "string", required=True, description="What to remind about"),
            ToolParam("at", "string", required=False, description="ISO 8601 datetime, e.g. 2026-06-21T15:00"),
            ToolParam("in_minutes", "int", required=False, description="Minutes from now"),
        ],
        permission=Permission.AUTO,
        execute=_remind
    ),
    "remember": Tool(
        name="remember",
        description="Save a durable fact to your own long-term memory notes (memory.md) — facts about Adnan, projects, decisions, environment. Not feelings, not chit-chat.",
        params=[
            ToolParam("note", "string", required=True, description="One concise, true, durable fact worth carrying forward"),
        ],
        permission=Permission.AUTO,
        execute=_remember
    ),
    "forget": Tool(
        name="forget",
        description="Remove a single note from memory by matching a few of its words. Use THIS to forget one thing — never update_memory.",
        params=[
            ToolParam("query", "string", required=True, description="A few words that identify the note to drop, e.g. 'chicken popcorn'"),
        ],
        permission=Permission.AUTO,
        execute=_forget
    ),
    "update_memory": Tool(
        name="update_memory",
        description="DANGER: replaces your ENTIRE memory body. Only to rewrite the whole curated list — you must include every note you want to keep. To remove one item use forget instead.",
        params=[
            ToolParam("content", "string", required=True, description="The FULL curated notes body (every bullet you want to keep)"),
        ],
        permission=Permission.AUTO,
        execute=_update_memory
    ),
    "list_projects": Tool(
        name="list_projects",
        description="List the codebases/projects you can open, and which one is currently loaded.",
        params=[],
        permission=Permission.AUTO,
        execute=_list_projects
    ),
    "open_project": Tool(
        name="open_project",
        description="Load a project/codebase into context so you can read and reason about its files. Do this yourself whenever a task or question needs a project you don't have loaded. Accepts an exact or partial name.",
        params=[
            ToolParam("name", "string", required=True, description="Project name (exact or partial), e.g. 'prana'"),
        ],
        permission=Permission.AUTO,
        execute=_open_project
    ),
}


def get_tool_schemas() -> str:
    """Return tool descriptions for the system prompt."""
    schemas = []
    for tool in TOOLS.values():
        params_desc = ", ".join([
            f"{p.name}: {p.type}{'?' if not p.required else ''}"
            for p in tool.params
        ])
        schemas.append(f'- {tool.name}({params_desc}): {tool.description}')
    return "\n".join(schemas)


def get_tools_for_prompt() -> str:
    """Tool list + permission, for injecting into an agent system prompt."""
    lines = []
    for tool in TOOLS.values():
        params_desc = ", ".join(
            f"{p.name}: {p.type}{'?' if not p.required else ''}" for p in tool.params
        )
        gate = "auto" if tool.permission == Permission.AUTO else "needs approval"
        lines.append(f"- {tool.name}({params_desc}) [{gate}]: {tool.description}")
    return "\n".join(lines)


_JSON_TYPE = {"string": "string", "int": "integer", "number": "number",
              "bool": "boolean", "list": "array"}


def get_tools_openai_schema() -> list[dict]:
    """Kamil's tools as OpenAI/JSON-schema function definitions, for NATIVE
    tool-calling (passing `tools` to llama-server so the model returns structured
    tool_calls instead of a hand-formatted ```tool fence)."""
    out = []
    for tool in TOOLS.values():
        props, required = {}, []
        for p in tool.params:
            schema = {"type": _JSON_TYPE.get(p.type, "string")}
            if p.description:
                schema["description"] = p.description
            if p.type == "list":
                schema["items"] = {"type": "string"}
            props[p.name] = schema
            if p.required:
                required.append(p.name)
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return out


def validate_tool_call(tool_name: str, params: dict) -> tuple[bool, str]:
    """Validate a tool call against its schema."""
    if tool_name not in TOOLS:
        return False, f"Unknown tool: {tool_name}"
    
    tool = TOOLS[tool_name]
    for param in tool.params:
        if param.required and param.name not in params:
            return False, f"Missing required param: {param.name}"
    
    return True, ""


def execute_tool(tool_name: str, params: dict) -> ToolResult:
    """Execute a tool by name with params."""
    if tool_name not in TOOLS:
        return ToolResult(success=False, error=f"Unknown tool: {tool_name}", tool=tool_name, params=params)
    
    valid, error = validate_tool_call(tool_name, params)
    if not valid:
        return ToolResult(success=False, error=error, tool=tool_name, params=params)
    
    return TOOLS[tool_name].execute(params)


def requires_confirmation(tool_name: str) -> bool:
    if tool_name not in TOOLS:
        return True
    return TOOLS[tool_name].permission == Permission.CONFIRM


def requires_confirmation_call(tool_name: str, params: dict) -> bool:
    """Call-aware confirmation for autonomous agent mode: a CONFIRM tool may
    auto-run if THIS specific call already passes the hard guardrails (write inside
    the allowed roots, shell not on the denylist). Calls that fall outside the
    fence still pause for the user."""
    tool = TOOLS.get(tool_name)
    if tool is None:
        return True
    if tool.permission != Permission.CONFIRM:
        return False
    params = params or {}
    if tool_name in ("write_file", "edit_file"):
        # Writes are gated by the write-root fence at EXECUTION, not by a prompt:
        # in-root -> runs and writes; out-of-root (incl. Kamil's own C:\kamil source)
        # -> runs and is hard-refused. Never pause here — prompting to "approve" an
        # out-of-root write is misleading, since it's refused either way.
        return False
    if tool_name == "shell":
        ok, _ = _command_allowed(params.get("command", ""))
        return not ok
    return True  # any other approval-gated tool still confirms


def _extract_json_objects(text: str) -> list[str]:
    """Yield every top-level {...} substring, correctly skipping braces, backticks,
    and quotes that appear INSIDE JSON string literals — so a write_file whose
    content contains code, braces, or ``` fences doesn't fool the boundary scan."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != '{':
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        closed = False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    out.append(text[i:j + 1])
                    i = j + 1
                    closed = True
                    break
            j += 1
        if not closed:
            break   # unbalanced (truncated) — stop
    return out


def _repair_json(raw: str) -> str:
    """Escape literal newlines/tabs that sit INSIDE string literals — the most
    common way a model breaks otherwise-valid tool-call JSON (multi-line file
    content with real line breaks instead of \\n)."""
    out = []
    in_str = False
    esc = False
    for c in raw:
        if in_str:
            if esc:
                out.append(c); esc = False
            elif c == '\\':
                out.append(c); esc = True
            elif c == '"':
                out.append(c); in_str = False
            elif c == '\n':
                out.append('\\n')
            elif c == '\r':
                out.append('\\r')
            elif c == '\t':
                out.append('\\t')
            else:
                out.append(c)
        else:
            out.append(c)
            if c == '"':
                in_str = True
    return ''.join(out)


def _loads_lenient(raw: str):
    """json.loads with a couple of forgiving fallbacks (control chars in strings,
    trailing commas)."""
    import re
    for candidate in (raw, _repair_json(raw)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    try:
        return json.loads(re.sub(r',\s*([}\]])', r'\1', _repair_json(raw)))
    except (json.JSONDecodeError, ValueError):
        return None


import re as _re_fn
# Llama-style tool syntax some models emit as TEXT instead of native tool_calls:
#   <function=list_directory><parameter=path>C:\...\prana</parameter></function>
# Unparsed, it leaks into the answer AND the model loops (it never "sees" a result).
_FUNC_TAG = _re_fn.compile(r'<function\s*=\s*([A-Za-z_]\w*)\s*>(.*?)(?:</function>|$)', _re_fn.DOTALL)
_PARAM_TAG = _re_fn.compile(r'<parameter\s*=\s*([A-Za-z_]\w*)\s*>(.*?)(?:</parameter>|$)', _re_fn.DOTALL)


def _parse_function_tags(response: str) -> list[dict]:
    out = []
    for m in _FUNC_TAG.finditer(response):
        body = m.group(2)
        params = {k: v.strip() for k, v in _PARAM_TAG.findall(body)}
        out.append({"tool": m.group(1), "params": params})
    return out


def parse_tool_calls(response: str) -> list[dict]:
    """
    Parse tool calls from a model response, robust to how the model formats them:
    fenced (```tool / ```json / ```), bare JSON, the Llama <function=...> tag syntax,
    and — crucially — write_file/edit_file calls whose content contains code, braces,
    or literal newlines. Boundary detection is string-aware and JSON parsing is
    lenient. Each call is normalised to {"tool", "params"}.
    """
    calls: list[dict] = []
    seen: set[str] = set()
    for norm in _parse_function_tags(response):
        key = json.dumps(norm, sort_keys=True)
        if key not in seen:
            seen.add(key)
            calls.append(norm)
    for raw in _extract_json_objects(response):
        if '"tool"' not in raw and "'tool'" not in raw:
            continue
        call = _loads_lenient(raw)
        if not isinstance(call, dict) or "tool" not in call:
            continue
        call.setdefault("params", {})
        if not isinstance(call["params"], dict):
            continue
        norm = {"tool": call["tool"], "params": call["params"]}
        key = json.dumps(norm, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        calls.append(norm)
    return calls