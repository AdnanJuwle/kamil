"""Tests for core/paths.py — the cross-platform path/platform layer.

These guard the migration off Windows: every one of them would have been
impossible to write before, because the paths were string literals.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

from core import paths


def _reload(monkeypatch, **env):
    """Re-import core.paths with a given environment, since most values are
    resolved once at import time."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return importlib.reload(paths)


@pytest.fixture(autouse=True)
def restore():
    yield
    importlib.reload(paths)       # leave the module as the rest of the suite expects


# ── root detection ────────────────────────────────────────────────

def test_root_defaults_to_the_repo(monkeypatch):
    p = _reload(monkeypatch, KAMIL_ROOT=None)
    assert (p.KAMIL_ROOT / "core" / "paths.py").is_file()


def test_kamil_root_env_wins(monkeypatch, tmp_path):
    p = _reload(monkeypatch, KAMIL_ROOT=str(tmp_path))
    assert p.KAMIL_ROOT == tmp_path.resolve()
    assert p.DATA_DIR == tmp_path.resolve() / "data"
    assert p.MODELS_DIR == tmp_path.resolve() / "models"


def test_data_and_models_overridable(monkeypatch, tmp_path):
    p = _reload(monkeypatch,
                KAMIL_DATA_DIR=str(tmp_path / "d"),
                KAMIL_MODELS_DIR=str(tmp_path / "m"))
    assert p.DATA_DIR == tmp_path / "d"
    assert p.MODELS_DIR == tmp_path / "m"


def test_data_path_joins_without_separators(monkeypatch, tmp_path):
    p = _reload(monkeypatch, KAMIL_DATA_DIR=str(tmp_path))
    got = p.data_path("room", "x.mp4")
    assert Path(got) == tmp_path / "room" / "x.mp4"
    # no hand-rolled separators, so it is native on whatever OS runs this
    assert got == str(tmp_path / "room" / "x.mp4")


# ── no hard-coded drive letters anywhere ──────────────────────────

#: The packages that make up Kamil itself. Scanned for drive letters.
_APP_DIRS = ("core", "actions", "memory", "context", "interfaces", "models")

#: Directories inside those packages that are not Kamil's code.
_SKIP_PARTS = {"__pycache__", "node_modules", ".pytest_cache"}

# core/paths.py is the ONE module allowed to name a drive letter: it exists to
# contain the platform-specific knowledge the rest of the tree must not have.
_ALLOWED = {"paths.py"}


def _app_sources():
    root = Path(paths.__file__).resolve().parent.parent
    for d in _APP_DIRS:
        for py in (root / d).rglob("*.py"):
            if any(part in _SKIP_PARTS for part in py.relative_to(root).parts):
                continue
            if py.name in _ALLOWED:
                continue
            yield root, py
    run = root / "run.py"
    if run.is_file():
        yield root, run


def test_app_sources_exist_to_scan():
    """Guard against the scan below silently covering nothing.

    It did exactly that once: the skip list contained "kamil", and because the
    repository root is itself named kamil, every path matched and the test passed
    while real hard-coded paths sat in orchestrator.py and indexer.py.
    """
    found = list(_app_sources())
    assert len(found) >= 20, f"only {len(found)} source files scanned -- scan is broken"
    names = {py.name for _r, py in found}
    for expected in ("orchestrator.py", "tools.py", "indexer.py", "app.py"):
        assert expected in names, f"{expected} not reached by the scan"


def test_no_literal_windows_paths_in_source():
    """The whole point of the migration: outside core/paths.py, no module may name
    a drive letter -- not in code, and not in a prompt string that would send the
    model looking for C:\\kamil on a Fedora box."""
    offenders = []
    for root, py in _app_sources():
        for lineno, line in enumerate(
                py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue                      # comments may discuss the old layout
            if "C:\\" in line or "C:/" in line:
                offenders.append(f"{py.relative_to(root)}:{lineno}: {stripped[:80]}")
    assert not offenders, (
        f"{len(offenders)} hard-coded Windows path(s) found:\n" + "\n".join(offenders))


# ── binaries ──────────────────────────────────────────────────────

def test_llama_server_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\n")
    p = _reload(monkeypatch, KAMIL_LLAMA_SERVER=str(fake))
    assert p.llama_server_bin() == str(fake)


def test_llama_server_falls_back_to_bare_name(monkeypatch):
    """When nothing is found we return the bare name, so the spawn error names the
    real problem instead of a stale drive letter."""
    p = _reload(monkeypatch, KAMIL_LLAMA_SERVER=None)
    monkeypatch.setattr(p.shutil, "which", lambda _n: None)
    monkeypatch.setattr(p, "_first_existing", lambda _c: None)
    assert p.llama_server_bin() == "llama-server" + p.EXE


def test_venv_python_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "python3"
    fake.write_text("")
    p = _reload(monkeypatch, KAMIL_PYTHON=str(fake))
    assert p.venv_python() == str(fake)


def test_venv_python_falls_back_to_running_interpreter(monkeypatch, tmp_path):
    p = _reload(monkeypatch, KAMIL_PYTHON=None, KAMIL_ROOT=str(tmp_path))
    assert p.venv_python() == sys.executable


# ── platform branching ────────────────────────────────────────────

def test_exactly_one_platform_flag():
    assert sum([paths.IS_WINDOWS, paths.IS_LINUX, paths.IS_MACOS]) == 1
    assert paths.IS_POSIX == (not paths.IS_WINDOWS)


def test_kill_process_tree_is_platform_appropriate():
    argv = paths.kill_process_tree_cmd(4321)
    assert "4321" in " ".join(argv)
    assert argv[0] == ("taskkill" if paths.IS_WINDOWS else "pkill")


def test_free_port_cmd_mentions_the_port():
    argv = paths.free_port_cmd(8080)
    assert "8080" in " ".join(argv)
    assert argv[0] == ("powershell" if paths.IS_WINDOWS else "sh")


def test_default_shell_is_none_only_on_windows():
    sh = paths.default_shell()
    if paths.IS_WINDOWS:
        assert sh is None
    else:
        assert sh and sh.startswith("/")


def test_system_root_prefixes_are_lowercase_and_nonempty():
    prefixes = paths.system_root_prefixes()
    assert prefixes
    assert all(p == p.lower() for p in prefixes)
    if not paths.IS_WINDOWS:
        assert "/usr" in prefixes
        assert "/etc/systemd" in prefixes


def test_disk_root_is_a_real_directory():
    assert os.path.isdir(paths.disk_root())


def test_projects_root_is_absolute():
    assert Path(paths.PROJECTS_ROOT).is_absolute()


def test_projects_root_env_wins(monkeypatch, tmp_path):
    p = _reload(monkeypatch, KAMIL_PROJECTS_ROOT=str(tmp_path))
    assert p.PROJECTS_ROOT == tmp_path
