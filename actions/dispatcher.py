import re
import json
from actions import filesystem, shell, tasks

ACTION_PATTERN = re.compile(
    r'<action\s+type="([^"]+)"([^>]*)(?:/>|>(.*?)</action>)',
    re.DOTALL
)

ATTR_PATTERN = re.compile(r'(\w+)="([^"]*)"')

def parse_actions(text: str) -> tuple[str, list[dict]]:
    actions = []
    def replacer(match):
        action_type = match.group(1)
        attrs_str = match.group(2)
        body = match.group(3) or ""
        attrs = dict(ATTR_PATTERN.findall(attrs_str))
        attrs["type"] = action_type
        attrs["body"] = body.strip()
        actions.append(attrs)
        return f"[ACTION:{action_type}]"
    clean_text = ACTION_PATTERN.sub(replacer, text)
    return clean_text, actions

def requires_confirmation(action: dict) -> bool:
    return action["type"] in {"write_file", "edit_file", "shell", "delete_task"}

def execute_action(action: dict) -> dict:
    t = action["type"]
    if t == "read_file":
        return filesystem.read_file(action.get("path", ""))
    elif t == "list_directory":
        return filesystem.list_directory(
            action.get("path", ""),
            recursive=action.get("recursive", "false").lower() == "true"
        )
    elif t == "write_file":
        return filesystem.write_file(
            action.get("path", ""),
            action.get("body", ""),
            overwrite=action.get("overwrite", "true").lower() == "true"
        )
    elif t == "edit_file":
        return filesystem.edit_file(
            action.get("path", ""),
            action.get("old", ""),
            action.get("new", action.get("body", ""))
        )
    elif t == "shell":
        return shell.execute(
            action.get("command", ""),
            cwd=action.get("cwd", None),
            timeout=int(action.get("timeout", "30"))
        )
    elif t == "create_task":
        return tasks.create_task(
            action.get("title", ""),
            action.get("description", action.get("body", "")),
            action.get("project", "")
        )
    elif t == "list_tasks":
        return tasks.list_tasks(
            project=action.get("project", None),
            status=action.get("status", None)
        )
    elif t == "complete_task":
        return tasks.complete_task(int(action.get("id", 0)))
    else:
        return {"success": False, "error": f"Unknown action type: {t}"}