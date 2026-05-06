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
from dataclasses import dataclass
from typing import Any, Callable, Optional
from enum import Enum


class Permission(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


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


def _write_file(params: dict) -> ToolResult:
    path = params["path"]
    content = params["content"]
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
        import sys
        sys.path.insert(0, r'C:\kamil')
        from actions.tasks import add_task
        task = add_task(
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


def parse_tool_calls(response: str) -> list[dict]:
    """
    Parse tool calls from model response.
    Model should output JSON blocks like:
```tool
    {"tool": "read_file", "params": {"path": "C:\\file.py"}}
```
    """
    import re
    calls = []
    pattern = r'```tool\s*\n(.*?)\n```'
    matches = re.findall(pattern, response, re.DOTALL)
    for match in matches:
        try:
            call = json.loads(match.strip())
            if "tool" in call and "params" in call:
                calls.append(call)
        except json.JSONDecodeError:
            pass
    return calls