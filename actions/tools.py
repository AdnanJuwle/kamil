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
# Reads are allowed broadly; WRITES and SHELL are fenced to these roots so an
# agent loop can't clobber system files or Kamil's own code by accident.
# Override with KAMIL_WRITE_ROOTS (semicolon-separated absolute paths).
_DEFAULT_WRITE_ROOTS = r"C:\kamil\workspace;C:\Users\adnan\Documents\repos"

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
    for root in _write_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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
            success=False,
            error=f"refused: '{path}' is outside the allowed write roots ({'; '.join(str(r) for r in _write_roots())})",
            tool="write_file", params=params
        )
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
            error=f"refused: '{path}' is outside the allowed write roots",
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
            ToolParam("timeout", "int", required=False, description="Timeout in seconds", default=30)
        ],
        permission=Permission.CONFIRM,
        execute=_shell
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
        return not _within_write_roots(params.get("path", ""))
    if tool_name == "shell":
        ok, _ = _command_allowed(params.get("command", ""))
        return not ok
    return True  # any other approval-gated tool still confirms


def parse_tool_calls(response: str) -> list[dict]:
    """
    Parse tool calls from a model response. Tolerant of how the model fences them:
    accepts ```tool / ```json / plain ``` blocks, and bare JSON objects that
    carry a "tool" key. Each accepted call is normalised to {"tool", "params"}.
    """
    import re
    calls: list[dict] = []
    seen: set[str] = set()

    def _accept(raw: str):
        raw = raw.strip()
        if not raw:
            return
        try:
            call = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(call, dict) or "tool" not in call:
            return
        call.setdefault("params", {})
        if not isinstance(call["params"], dict):
            return
        key = json.dumps(call, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        calls.append({"tool": call["tool"], "params": call["params"]})

    # 1) fenced code blocks: ```tool / ```json / ```
    for block in re.findall(r'```(?:tool|json)?\s*\n(.*?)```', response, re.DOTALL):
        _accept(block)

    # 2) bare top-level JSON objects that mention a tool (balanced-brace scan)
    if not calls:
        for m in re.finditer(r'\{', response):
            start = m.start()
            depth = 0
            for i in range(start, len(response)):
                c = response[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = response[start:i + 1]
                        if '"tool"' in candidate:
                            _accept(candidate)
                        break
    return calls