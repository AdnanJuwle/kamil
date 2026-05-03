import subprocess
import shlex
import os

BLOCKED_COMMANDS = {
    'rm', 'rmdir', 'del', 'format', 'shutdown', 'reboot',
    'rd', 'mkfs', 'dd', 'fdisk'
}

def _is_safe_command(command: str) -> tuple[bool, str]:
    first_token = command.strip().split()[0].lower()
    if first_token in BLOCKED_COMMANDS:
        return False, f"Command '{first_token}' is blocked for safety"
    return True, ""

def execute(command: str, cwd: str = None, timeout: int = 30) -> dict:
    safe, reason = _is_safe_command(command)
    if not safe:
        return {"success": False, "error": reason, "stdout": "", "stderr": ""}
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd()
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
            "command": command
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s", "stdout": "", "stderr": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "stdout": "", "stderr": ""}