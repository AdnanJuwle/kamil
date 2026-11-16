"""Tests for system-control mode in actions/tools.py.

On Fedora Kamil owns the machine: the write-root fence lifts, sudo and systemd are
in reach, and the whole filesystem is writable. What must survive that is the
destructive-command gate — rm -rf, dd, mkfs and friends still pause for approval
rather than running unattended. These tests pin exactly that boundary, on any OS.
"""
import pytest

from actions import tools


@pytest.fixture
def owned(monkeypatch):
    """Kamil owns this machine (the Fedora posture)."""
    monkeypatch.setenv("KAMIL_SYSTEM_CONTROL", "1")
    tools.set_active_project_dir(None)
    yield
    tools.set_active_project_dir(None)


@pytest.fixture
def fenced(monkeypatch, tmp_path):
    """Kamil is a guest (the Windows posture)."""
    monkeypatch.setenv("KAMIL_SYSTEM_CONTROL", "0")
    monkeypatch.setenv("KAMIL_WRITE_ROOTS", str(tmp_path))
    tools.set_active_project_dir(None)
    yield tmp_path
    tools.set_active_project_dir(None)


# ── the switch ────────────────────────────────────────────────────

def test_env_forces_the_mode(monkeypatch):
    monkeypatch.setenv("KAMIL_SYSTEM_CONTROL", "1")
    assert tools.system_control() is True
    monkeypatch.setenv("KAMIL_SYSTEM_CONTROL", "0")
    assert tools.system_control() is False
    for falsey in ("", "no", "off", "false", "FALSE"):
        monkeypatch.setenv("KAMIL_SYSTEM_CONTROL", falsey)
        assert tools.system_control() is False


def test_defaults_to_linux_only(monkeypatch):
    monkeypatch.delenv("KAMIL_SYSTEM_CONTROL", raising=False)
    assert tools.system_control() is tools._paths.IS_LINUX


# ── destructive detection: the part that must not regress ─────────

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf /home/adnan",
    "sudo rm -rf /var/log",
    "doas rm -r /etc",
    "pkexec rm -rf /opt",
    "env FOO=1 sudo -n poweroff",
    "sudo dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/nvme0n1",
    "sudo wipefs -a /dev/sda",
    "shutdown -h now",
    "sudo systemctl mask sshd",
    "chmod -R 777 /",
    "sudo userdel adnan",
    "dnf remove -y kernel",
    "sudo rpm -e glibc",
    ":(){ :|:& };:",
    "git push --force",
])
def test_destructive_commands_are_recognised(command):
    destructive, reason = tools._command_is_destructive(command)
    assert destructive is True, f"{command!r} was not flagged"
    assert reason


@pytest.mark.parametrize("command", [
    "ls -la /etc",
    "cat /etc/os-release",
    "systemctl status sshd",
    "systemctl restart sshd",
    "dnf install -y vim",
    "journalctl -u kamil -n 50",
    "git status",
    "ps aux",
    "nvidia-smi",
    "df -h",
    "sudo dnf install -y ffmpeg",
    "python -m pytest",
    "grep -r TODO .",
])
def test_ordinary_commands_are_not_flagged(command):
    destructive, _reason = tools._command_is_destructive(command)
    assert destructive is False, f"{command!r} was wrongly flagged"


def test_empty_command_is_not_destructive_but_is_refused():
    assert tools._command_is_destructive("") == (False, "")
    ok, reason = tools._command_allowed("   ")
    assert ok is False
    assert "empty" in reason


# ── fenced vs owned: allow/refuse ─────────────────────────────────

def test_fenced_mode_refuses_destructive_outright(fenced):
    ok, reason = tools._command_allowed("rm -rf /")
    assert ok is False
    assert "safety" in reason


def test_owned_mode_allows_everything_at_execution(owned):
    """Under system control nothing is refused for being destructive -- the user
    approves it instead, which is the difference between Kamil declining and Kamil
    asking."""
    for cmd in ("rm -rf /tmp/x", "sudo dd if=/dev/zero of=/dev/sdz", "poweroff"):
        ok, reason = tools._command_allowed(cmd)
        assert ok is True, f"{cmd!r} refused: {reason}"


def test_owned_mode_still_pauses_for_destructive(owned):
    rc = tools.requires_confirmation_call
    assert rc("shell", {"command": "rm -rf /home/adnan"}) is True
    assert rc("shell", {"command": "sudo poweroff"}) is True
    assert rc("shell", {"command": "ls /etc"}) is False
    assert rc("shell", {"command": "systemctl status sshd"}) is False


# ── the write fence ───────────────────────────────────────────────

def test_fence_lifts_under_system_control(owned):
    for p in ("/etc/hosts", "/usr/local/bin/x", "/home/adnan/anything"):
        assert tools._within_write_roots(p) is True


def test_fence_holds_when_not_in_control(fenced):
    inside = str(fenced / "ok.py")
    assert tools._within_write_roots(inside) is True
    outside = str(fenced.parent / "elsewhere" / "no.py")
    assert tools._within_write_roots(outside) is False


def test_project_fence_does_not_block_system_work(owned, tmp_path):
    """With a project open, Kamil must still be able to touch /etc -- otherwise
    loading a project would silently revoke system control."""
    tools.set_active_project_dir(str(tmp_path))
    assert tools._within_write_roots("/etc/hosts") is True


def test_shell_may_write_files_under_system_control(owned):
    """sed -i on a unit file IS the job when Kamil administers the box."""
    assert tools._is_shell_file_write("sed -i s/a/b/ /etc/fstab") is False
    assert tools._is_shell_file_write("echo x > /etc/motd") is False


def test_shell_file_writes_blocked_when_fenced(fenced):
    assert tools._is_shell_file_write("sed -i s/a/b/ x.conf") is True
    assert tools._is_shell_file_write("tee /etc/motd") is True


# ── secrets stay off-limits in both modes ─────────────────────────

def test_secrets_are_never_readable(owned):
    """System control is about the machine, not about Kamil reading its own sealed
    secrets. That block is a deliberate design guarantee, not a sandbox."""
    for cmd in ("cat .env", "type code13\\will.sealed", "cat kamil.key"):
        assert tools._shell_touches_secret(cmd) is True


# ── the privileged tools ──────────────────────────────────────────

def test_privileged_tools_refuse_without_system_control(fenced):
    for name, params in (("service_control", {"action": "stop", "unit": "sshd"}),
                         ("package_manager", {"action": "install", "package": "vim"}),
                         ("process_control", {"action": "kill", "target": "1"})):
        res = tools.execute_tool(name, params)
        assert res.success is False
        assert "system control is off" in res.error


@pytest.mark.parametrize("name,read_params,write_params", [
    ("service_control", {"action": "status", "unit": "sshd"},
     {"action": "stop", "unit": "sshd"}),
    ("package_manager", {"action": "search", "package": "vim"},
     {"action": "install", "package": "vim"}),
    ("process_control", {"action": "list"}, {"action": "kill", "target": "1"}),
])
def test_read_only_calls_run_but_writes_pause(owned, name, read_params, write_params):
    rc = tools.requires_confirmation_call
    assert rc(name, read_params) is False, f"{name} {read_params} should not pause"
    assert rc(name, write_params) is True, f"{name} {write_params} must pause"


def test_unknown_verbs_are_rejected_with_the_valid_list(owned):
    res = tools.execute_tool("service_control", {"action": "obliterate", "unit": "x"})
    assert res.success is False
    assert "unknown action" in res.error
    assert "restart" in res.error        # tells the model what IS valid


def test_service_control_needs_a_unit(owned):
    res = tools.execute_tool("service_control", {"action": "restart"})
    assert res.success is False
    assert "unit name is required" in res.error


def test_process_control_list_works(owned):
    res = tools.execute_tool("process_control", {"action": "list"})
    assert res.success is True
    assert res.output["count"] > 0
    assert "pid" in res.output["processes"][0]


def test_process_control_find_filters_by_name(owned):
    res = tools.execute_tool("process_control", {"action": "find", "target": "python"})
    assert res.success is True
    for p in res.output["processes"]:
        assert "python" in (p["name"] or "").lower()


def test_process_control_kill_needs_a_target(owned):
    res = tools.execute_tool("process_control", {"action": "kill"})
    assert res.success is False
    assert "pid or process name is required" in res.error


def test_process_control_reports_no_match(owned):
    res = tools.execute_tool("process_control",
                             {"action": "kill", "target": "definitely-not-a-process"})
    assert res.success is False
    assert "no process matched" in res.error


def test_package_manager_needs_a_package(owned):
    res = tools.execute_tool("package_manager", {"action": "install"})
    assert res.success is False
    assert "package name is required" in res.error


def test_sudo_prefix_only_on_posix_and_only_when_not_root():
    argv = tools._sudo(["systemctl", "restart", "sshd"])
    if tools._paths.IS_WINDOWS:
        assert argv[0] == "systemctl"
    else:
        import os
        if os.geteuid() == 0:
            assert argv[0] == "systemctl"
        else:
            assert argv[:2] == ["sudo", "-n"]


# ── system_info is platform-correct ───────────────────────────────

def test_system_info_reports_this_machine():
    res = tools.execute_tool("system_info", {})
    assert res.success is True
    info = res.output
    assert info["os"] == tools._paths.SYSTEM
    if "disk_free_gb" in info:
        assert info["disk_free_gb"] > 0          # not a hard-coded C:\ any more
        assert "system_control" in info
