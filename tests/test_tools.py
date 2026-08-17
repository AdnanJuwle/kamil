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


# ── grep / read_lines ─────────────────────────────────────────────

def test_grep_finds_content(tmp_path):
    (tmp_path / "a.py").write_text("import os\ndef foo():\n    return SECRET_XYZ\n")
    (tmp_path / "b.txt").write_text("nothing here\n")
    res = tools.execute_tool("grep", {"pattern": "SECRET_XYZ", "directory": str(tmp_path)})
    assert res.success is True
    m = res.output["matches"]
    assert len(m) == 1 and m[0]["line"] == 3 and "a.py" in m[0]["file"]


def test_grep_glob_filter(tmp_path):
    (tmp_path / "a.py").write_text("target\n")
    (tmp_path / "a.md").write_text("target\n")
    res = tools.execute_tool("grep", {"pattern": "target", "directory": str(tmp_path), "glob": "*.py"})
    assert len(res.output["matches"]) == 1 and res.output["matches"][0]["file"].endswith(".py")


def test_grep_bad_regex(tmp_path):
    res = tools.execute_tool("grep", {"pattern": "([", "directory": str(tmp_path)})
    assert res.success is False and "regex" in res.error


def test_read_lines_range(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("".join(f"line{i}\n" for i in range(1, 51)))
    res = tools.execute_tool("read_lines", {"path": str(f), "start": 10, "end": 12})
    assert res.success is True
    assert "line10" in res.output["content"] and "line12" in res.output["content"]
    assert "line13" not in res.output["content"]
    assert res.output["total_lines"] == 50


# ── remind tool ───────────────────────────────────────────────────

def test_remind_in_minutes(monkeypatch, tmp_path):
    import core.reminders as rem
    monkeypatch.setattr(rem, "REMINDERS_PATH", str(tmp_path / "reminders.json"))
    res = tools.execute_tool("remind", {"text": "stand up", "in_minutes": 30})
    assert res.success is True
    assert res.output["text"] == "stand up"
    assert len(rem.list_reminders()) == 1


def test_remind_requires_a_time(monkeypatch, tmp_path):
    import core.reminders as rem
    monkeypatch.setattr(rem, "REMINDERS_PATH", str(tmp_path / "reminders.json"))
    res = tools.execute_tool("remind", {"text": "no time given"})
    assert res.success is False
    assert "in_minutes" in res.error or "at" in res.error


# ── permission mapping ────────────────────────────────────────────

def test_permission_levels():
    assert tools.requires_confirmation("read_file") is False
    assert tools.requires_confirmation("write_file") is True
    assert tools.requires_confirmation("edit_file") is True
    assert tools.requires_confirmation("shell") is True
    assert tools.requires_confirmation("unknown_tool") is True  # unknown -> safe default


def test_call_aware_confirmation_in_vs_out_of_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("KAMIL_WRITE_ROOTS", str(tmp_path))
    inside = str(tmp_path / "a.py")
    rc = tools.requires_confirmation_call
    assert rc("read_file", {}) is False                          # auto tool
    assert rc("edit_file", {"path": inside}) is False            # in fence -> auto
    assert rc("write_file", {"path": inside}) is False           # in fence -> auto
    assert rc("edit_file", {"path": "C:\\Windows\\x.py"}) is True  # outside -> pause
    assert rc("shell", {"command": "echo hi"}) is False          # allowed -> auto
    assert rc("shell", {"command": "rm -rf C:\\"}) is True        # denylisted -> pause


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
