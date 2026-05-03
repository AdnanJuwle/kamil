import os
from pathlib import Path

SAFE_ROOT = "C:\\"

def _is_safe_path(path: str) -> bool:
    resolved = Path(path).resolve()
    safe = Path(SAFE_ROOT).resolve()
    return str(resolved).startswith(str(safe))

def read_file(path: str) -> dict:
    if not _is_safe_path(path):
        return {"success": False, "error": f"Path outside safe root: {path}"}
    p = Path(path)
    if not p.exists():
        return {"success": False, "error": f"File not found: {path}"}
    if not p.is_file():
        return {"success": False, "error": f"Not a file: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        return {
            "success": True,
            "path": str(p),
            "content": content,
            "lines": len(lines),
            "size_bytes": p.stat().st_size
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def write_file(path: str, content: str, overwrite: bool = True) -> dict:
    if not _is_safe_path(path):
        return {"success": False, "error": f"Path outside safe root: {path}"}
    p = Path(path)
    if p.exists() and not overwrite:
        return {"success": False, "error": f"File exists and overwrite=False: {path}"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True, "path": str(p), "bytes_written": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}

def edit_file(path: str, old_content: str, new_content: str) -> dict:
    if not _is_safe_path(path):
        return {"success": False, "error": f"Path outside safe root: {path}"}
    result = read_file(path)
    if not result["success"]:
        return result
    original = result["content"]
    if old_content not in original:
        return {"success": False, "error": "Target content not found in file — no changes made"}
    if original.count(old_content) > 1:
        return {"success": False, "error": "Target content appears multiple times — too ambiguous to edit safely"}
    updated = original.replace(old_content, new_content, 1)
    return write_file(path, updated)

def list_directory(path: str, recursive: bool = False) -> dict:
    if not _is_safe_path(path):
        return {"success": False, "error": f"Path outside safe root: {path}"}
    p = Path(path)
    if not p.exists():
        return {"success": False, "error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"success": False, "error": f"Not a directory: {path}"}
    try:
        entries = []
        if recursive:
            for item in p.rglob("*"):
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "file" if item.is_file() else "dir",
                    "size": item.stat().st_size if item.is_file() else None
                })
        else:
            for item in sorted(p.iterdir()):
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "file" if item.is_file() else "dir",
                    "size": item.stat().st_size if item.is_file() else None
                })
        return {"success": True, "path": str(p), "entries": entries}
    except Exception as e:
        return {"success": False, "error": str(e)}