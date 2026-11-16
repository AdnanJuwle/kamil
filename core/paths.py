r"""
core/paths.py -- the one place Kamil learns where it lives and what OS it is on.

Kamil was born on a single Windows box, so absolute paths like C:\kamil and
C:\llama.cpp\build\bin\llama-server.exe were hard-coded in ~50 places. Moving to
Fedora (or anywhere) meant editing all of them. This module replaces every one of
those literals with a resolved value, so the same tree runs on Linux, Windows and
macOS with no edits.

Resolution order is always: explicit env var -> platform-conventional location ->
sensible fallback. Nothing here touches the network.

Rules for the rest of the codebase:
  * never write a drive letter or a leading / in a module
  * never join paths with "\" or "/" by hand -- use os.path.join / pathlib
  * ask this module for binaries (llama_server_bin(), ffmpeg_bin(), venv_python())
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

# -- Platform -------------------------------------------------------
SYSTEM = platform.system()               # 'Windows' | 'Linux' | 'Darwin'
IS_WINDOWS = SYSTEM == "Windows"
IS_LINUX = SYSTEM == "Linux"
IS_MACOS = SYSTEM == "Darwin"
IS_POSIX = not IS_WINDOWS

EXE = ".exe" if IS_WINDOWS else ""       # suffix for native binaries


# -- Kamil's own tree -----------------------------------------------
def _detect_root() -> Path:
    """Kamil's source root. KAMIL_ROOT wins; otherwise it is the parent of core/."""
    env = os.getenv("KAMIL_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


KAMIL_ROOT: Path = _detect_root()

DATA_DIR = Path(os.getenv("KAMIL_DATA_DIR") or KAMIL_ROOT / "data")
MODELS_DIR = Path(os.getenv("KAMIL_MODELS_DIR") or KAMIL_ROOT / "models")
PROMPTS_DIR = Path(os.getenv("KAMIL_PROMPTS_DIR") or KAMIL_ROOT / "prompts")
VOICES_DIR = Path(os.getenv("KAMIL_VOICES_DIR") or KAMIL_ROOT / "voices")
WORKSPACE_DIR = Path(os.getenv("KAMIL_WORKSPACE_DIR") or KAMIL_ROOT / "workspace")
CODE13_DIR = KAMIL_ROOT / "code13"


def data_path(*parts: str) -> str:
    """A path inside data/, as a string (most callers hand this straight to open())."""
    return str(DATA_DIR.joinpath(*parts))


def model_path(*parts: str) -> str:
    return str(MODELS_DIR.joinpath(*parts))


def ensure_dirs() -> None:
    """Create the directories Kamil writes into. Safe to call repeatedly."""
    for d in (DATA_DIR, DATA_DIR / "threads_archive"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


# -- Where the user's editable projects live ------------------------
def _detect_projects_root() -> Path:
    """The coding-agent write root. KAMIL_PROJECTS_ROOT wins; otherwise the first
    existing conventional location for this OS; otherwise ~/repos."""
    env = os.getenv("KAMIL_PROJECTS_ROOT")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    candidates = [home / "repos", home / "Documents" / "repos",
                  home / "Projects", home / "projects", home / "src", home / "dev"]
    for c in candidates:
        if c.is_dir():
            return c
    return home / "repos"


PROJECTS_ROOT: Path = _detect_projects_root()


# -- Binaries -------------------------------------------------------
def _first_existing(paths) -> Path | None:
    for p in paths:
        try:
            p = Path(p).expanduser()
        except Exception:
            continue
        if p.is_file():
            return p
    return None


def llama_server_bin() -> str:
    """Absolute path to llama-server. KAMIL_LLAMA_SERVER wins, then PATH, then the
    usual build locations for this OS. Falls back to the bare name so the caller's
    spawn error names the real problem instead of a stale drive letter."""
    env = os.getenv("KAMIL_LLAMA_SERVER")
    if env:
        return str(Path(env).expanduser())
    found = shutil.which("llama-server")
    if found:
        return found
    home = Path.home()
    if IS_WINDOWS:
        cands = [Path("C:/llama.cpp/build/bin/llama-server.exe"),
                 home / "llama.cpp/build/bin/llama-server.exe",
                 Path("C:/Program Files/llama.cpp/bin/llama-server.exe")]
    else:
        cands = [Path("/usr/local/bin/llama-server"),
                 Path("/usr/bin/llama-server"),
                 Path("/opt/llama.cpp/build/bin/llama-server"),
                 home / ".local/bin/llama-server",
                 home / "llama.cpp/build/bin/llama-server",
                 Path("/opt/homebrew/bin/llama-server")]
    hit = _first_existing(cands)
    return str(hit) if hit else "llama-server" + EXE


def ffmpeg_bin() -> str:
    """ffmpeg, needed for webm->wav in transcription and voice enrollment."""
    return os.getenv("KAMIL_FFMPEG") or shutil.which("ffmpeg") or ("ffmpeg" + EXE)


def venv_python() -> str:
    """The interpreter inside Kamil's venv, whatever it is called on this OS."""
    env = os.getenv("KAMIL_PYTHON")
    if env:
        return str(Path(env).expanduser())
    sub = "Scripts" if IS_WINDOWS else "bin"
    for venv in ("venv313", "venv", ".venv"):
        base = KAMIL_ROOT / venv / sub
        for name in (("python.exe",) if IS_WINDOWS else ("python3", "python")):
            p = base / name
            if p.is_file():
                return str(p)
    return sys.executable


def tts_env_python() -> str:
    """The separate Chatterbox TTS env interpreter (its deps clash with the main venv)."""
    env = os.getenv("KAMIL_CHATTERBOX_PY")
    if env:
        return str(Path(env).expanduser())
    sub = "Scripts" if IS_WINDOWS else "bin"
    base = KAMIL_ROOT / "tts_env" / sub
    for name in (("python.exe",) if IS_WINDOWS else ("python3", "python")):
        p = base / name
        if p.is_file():
            return str(p)
    return venv_python()


# -- Shell / process differences ------------------------------------
def kill_process_tree_cmd(pid: int) -> list[str]:
    """Argv that kills `pid` and its children, per OS. Windows has no process
    groups the way POSIX does, hence taskkill /T."""
    if IS_WINDOWS:
        return ["taskkill", "/F", "/T", "/PID", str(pid)]
    return ["pkill", "-TERM", "-P", str(pid)]


def free_port_cmd(port: int) -> list[str]:
    """Last-resort argv to kill whatever is listening on `port`. The tiers try
    psutil first; this is the fallback for when psutil is missing or blind."""
    if IS_WINDOWS:
        return ["powershell", "-Command",
                f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                f"-ErrorAction SilentlyContinue | "
                f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"]
    # fuser is in psmisc on Fedora; lsof fallback covers minimal installs.
    return ["sh", "-c",
            f"fuser -k {port}/tcp 2>/dev/null || "
            f"lsof -ti tcp:{port} 2>/dev/null | xargs -r kill -9"]


def default_shell() -> str | None:
    """The executable subprocess(shell=True) should use. None = platform default."""
    if IS_WINDOWS:
        return None
    return os.getenv("SHELL") or "/bin/bash"


def system_root_prefixes() -> tuple[str, ...]:
    """Lowercased path prefixes that are the operating system itself rather than
    user data. The tool guardrails use this to recognise 'this is the OS' on any
    platform instead of pattern-matching C:\\Windows."""
    if IS_WINDOWS:
        sysroot = os.getenv("SystemRoot", r"C:\Windows").lower()
        return (sysroot, r"c:\program files", r"c:\program files (x86)", r"c:\programdata")
    return ("/boot", "/dev", "/proc", "/sys", "/usr", "/lib", "/lib64",
            "/bin", "/sbin", "/etc/systemd")


def disk_root() -> str:
    """The filesystem root to report free space for."""
    return str(KAMIL_ROOT.anchor or ("C:\\" if IS_WINDOWS else "/"))


def display(path) -> str:
    """A path written the way this OS writes it -- for prompts shown to the model."""
    try:
        return str(Path(path))
    except Exception:
        return str(path)


ensure_dirs()
