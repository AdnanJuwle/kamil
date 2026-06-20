"""Tests for actions/tools.py — guardrails, edit_file, the create_task fix, parsing."""
import pytest

from actions import tools


@pytest.fixture(autouse=True)
def write_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("KAMIL_WRITE_ROOTS", str(tmp_path))
    yield


# ── write fencing ─────────────────────────────────────────────────

def test_write_inside_root_ok(tmp_path):
    target = tmp_path / "note.txt"
    res = tools.execute_tool("write_file", {"path": str(target), "content": "hi"})
    assert res.success is True
    assert target.read_text() == "hi"


def test_write_outside_root_refused():
    res = tools.execute_tool("write_file", {"path": "C:\\Windows\\evil.txt", "content": "x"})
    assert res.success is False
    assert "outside the allowed write roots" in res.error


# ── edit_file ─────────────────────────────────────────────────────

def test_edit_file_unique_replace(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("a = 1\nb = 2\n")
    res = tools.execute_tool("edit_file", {"path": str(f), "old": "b = 2", "new": "b = 3"})
    assert res.success is True
    assert f.read_text() == "a = 1\nb = 3\n"


def test_edit_file_ambiguous_refused(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\nx = 1\n")
    res = tools.execute_tool("edit_file", {"path": str(f), "old": "x = 1", "new": "x = 9"})
    assert res.success is False
    assert "ambiguous" in res.error


def test_edit_file_missing_target(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("hello\n")
    res = tools.execute_tool("edit_file", {"path": str(f), "old": "nope", "new": "x"})
    assert res.success is False
    assert "not found" in res.error


# ── shell guardrails ──────────────────────────────────────────────

def test_shell_blocks_destructive_command():
    res = tools.execute_tool("shell", {"command": "rm -rf C:\\"})
    assert res.success is False
    assert "blocked" in res.error.lower() or "refused" in res.error.lower()


def test_shell_blocks_first_token_del():
    res = tools.execute_tool("shell", {"command": "del important.txt"})
    assert res.success is False


# ── create_task no longer raises (add_task -> create_task fix) ─────

def test_create_task_calls_backend(monkeypatch):
    captured = {}
    def fake_create_task(title, description="", project=""):
        captured.update(title=title, description=description, project=project)
        return {"success": True, "id": 1, "title": title}
    import actions.tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "create_task", fake_create_task)

    res = tools.execute_tool("create_task", {"title": "ship it", "project": "kamil"})
    assert res.success is True
    assert captured["title"] == "ship it"
    assert captured["project"] == "kamil"


# ── permission mapping ────────────────────────────────────────────

def test_permission_levels():
    assert tools.requires_confirmation("read_file") is False
    assert tools.requires_confirmation("write_file") is True
    assert tools.requires_confirmation("edit_file") is True
    assert tools.requires_confirmation("shell") is True
    assert tools.requires_confirmation("unknown_tool") is True  # unknown -> safe default


# ── tolerant parsing ──────────────────────────────────────────────

def test_parse_tool_fence():
    calls = tools.parse_tool_calls('```tool\n{"tool": "read_file", "params": {"path": "a"}}\n```')
    assert calls == [{"tool": "read_file", "params": {"path": "a"}}]


def test_parse_json_fence():
    calls = tools.parse_tool_calls('```json\n{"tool": "list_directory", "params": {"path": "a"}}\n```')
    assert calls[0]["tool"] == "list_directory"


def test_parse_bare_json_object():
    calls = tools.parse_tool_calls('Sure, let me look: {"tool": "read_file", "params": {"path": "a"}}')
    assert calls and calls[0]["tool"] == "read_file"


def test_parse_missing_params_defaults_empty():
    calls = tools.parse_tool_calls('```tool\n{"tool": "system_info"}\n```')
    assert calls == [{"tool": "system_info", "params": {}}]


def test_parse_ignores_non_tool_json():
    assert tools.parse_tool_calls('{"foo": "bar"}') == []
