from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

import control_freak.runtime as runtime
from control_freak.runtime import configure_web, read_service_enabled, set_service_enabled, web_health
from control_freak.web import read_state, update_state


@pytest.fixture(autouse=True)
def runtime_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "STATE_PATH", tmp_path / "web.json")


def test_web_health_uses_https_without_external_certificate_trust() -> None:
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def open_url(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    assert web_health(open_url=open_url) is True
    assert seen["url"] == "https://127.0.0.1:8787/health"
    assert seen["context"].check_hostname is False


def test_web_runtime_enables_and_starts_batocera_service(tmp_path: Path) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    state = configure_web(True, False, state_path=tmp_path / "web.json", run=run, health=lambda: True)

    assert state["enabled"] is True
    assert state["running"] is True
    assert state["pairing_token"]
    assert state["pairing_token"] not in (tmp_path / "web.json").read_text()
    assert commands == [
        ["/usr/bin/batocera-services", "enable", "control_freak"],
        ["/usr/bin/batocera-services", "start", "control_freak"],
    ]


def test_web_start_health_can_read_state_without_deadlock(tmp_path: Path) -> None:
    path = tmp_path / "web.json"

    state = configure_web(
        True,
        False,
        state_path=path,
        run=lambda _command, **_kwargs: SimpleNamespace(returncode=0),
        health=lambda: read_state(path)["enabled"],
    )

    assert state["enabled"] is True
    assert state["running"] is True


def test_web_service_action_runs_after_state_lock_is_released(tmp_path: Path) -> None:
    path = tmp_path / "web.json"

    def run(command, **_kwargs):
        if command[1] == "enable":
            return SimpleNamespace(returncode=0)
        probe = (
            "import fcntl,os,sys;"
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, str(tmp_path / "web.lock")],
            check=False,
            capture_output=True,
        )
        return SimpleNamespace(returncode=result.returncode)

    state = configure_web(True, False, state_path=path, run=run, health=lambda: True)

    assert state["running"] is True


def test_web_runtime_disables_and_invalidates_pairing(tmp_path: Path) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    configure_web(True, True, state_path=tmp_path / "web.json", run=run, health=lambda: True)
    commands.clear()
    state = configure_web(False, False, state_path=tmp_path / "web.json", run=run, health=lambda: False)

    assert state["enabled"] is False
    assert state["tailscale"] is False
    assert state["pair_hash"] == ""
    assert state["session_hash"] == ""
    assert state["running"] is False
    assert commands == []


def test_tailscale_change_does_not_restart_running_service(tmp_path: Path) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    configure_web(True, False, state_path=tmp_path / "web.json", run=run, health=lambda: True)
    commands.clear()
    state = configure_web(True, True, state_path=tmp_path / "web.json", run=run, health=lambda: True)

    assert state["tailscale"] is True
    assert commands == []


def test_service_enabled_uses_batocera_service_listing() -> None:
    def run(command, **kwargs):
        assert command == ["/usr/bin/batocera-services", "list", "user"]
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return SimpleNamespace(returncode=0, stdout="control_freak;*\nother;\n")

    assert read_service_enabled(run=run) is True


def test_service_enabled_preserves_disabled_semicolon_state() -> None:
    def run(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="other;*\ncontrol_freak;-\n")

    assert read_service_enabled(run=run) is False


def test_service_toggle_is_enable_start_and_stop_disable() -> None:
    commands = []
    service = {"enabled": False, "running": False}

    def run(command, **_kwargs):
        commands.append(command)
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        if action == "enable":
            service["enabled"] = True
        elif action == "disable":
            service["enabled"] = False
        elif action == "start":
            service["running"] = True
        elif action == "stop":
            service["running"] = False
        return SimpleNamespace(returncode=0)

    assert set_service_enabled(True, run=run) is True
    assert set_service_enabled(False, run=run, restore_audio=lambda: None) is False
    assert commands[2:4] == [
        ["/usr/bin/batocera-services", "enable", "control_freak"],
        ["/usr/bin/batocera-services", "start", "control_freak"],
    ]
    actions = [command for command in commands if command[1] not in {"list", "status"}]
    assert actions == [
        ["/usr/bin/batocera-services", "enable", "control_freak"],
        ["/usr/bin/batocera-services", "start", "control_freak"],
        ["/usr/bin/batocera-services", "stop", "control_freak"],
        ["/usr/bin/batocera-services", "disable", "control_freak"],
    ]


def test_failed_service_start_compensates_enablement() -> None:
    commands = []
    service = {"enabled": False, "running": False}

    def run(command, **_kwargs):
        commands.append(command)
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        if action == "enable":
            service["enabled"] = True
        elif action == "disable":
            service["enabled"] = False
        return SimpleNamespace(returncode=1 if action == "start" else 0)

    with pytest.raises(RuntimeError, match="start"):
        set_service_enabled(True, run=run)
    assert [command[1] for command in commands].count("disable") == 1
    assert service == {"enabled": False, "running": False}


def test_failed_disable_restores_exact_prior_service_state() -> None:
    service = {"enabled": True, "running": False}
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        if action == "disable":
            service["enabled"] = False
            return SimpleNamespace(returncode=1)
        if action == "enable":
            service["enabled"] = True
        elif action == "start":
            service["running"] = True
        elif action == "stop":
            service["running"] = False
        return SimpleNamespace(returncode=0)

    with pytest.raises(RuntimeError, match="disable"):
        set_service_enabled(False, run=run, restore_audio=lambda: None)

    assert service == {"enabled": True, "running": False}
    assert [command[1] for command in commands if command[1] not in {"list", "status"}] == ["disable", "enable"]


def test_service_disable_stops_and_verifies_before_restoring_audio() -> None:
    events = []
    service = {"enabled": True, "running": True}

    def run(command, **_kwargs):
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        events.append(action)
        if action == "stop":
            service["running"] = False
        elif action == "disable":
            service["enabled"] = False
        return SimpleNamespace(returncode=0)

    assert set_service_enabled(False, run=run, restore_audio=lambda: events.append("restore-audio")) is False
    assert events == ["stop", "restore-audio", "disable"]


def test_failed_disable_restores_exact_service_state_and_audio_policy() -> None:
    service = {"enabled": True, "running": True}
    audio = {"blocked": ["hdmi"]}

    def run(command, **_kwargs):
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        if action == "stop":
            service["running"] = False
        elif action == "start":
            service["running"] = True
        elif action == "disable":
            service["enabled"] = False
            return SimpleNamespace(returncode=1)
        elif action == "enable":
            service["enabled"] = True
        return SimpleNamespace(returncode=0)

    def restore_audio():
        before = list(audio["blocked"])
        audio["blocked"] = []
        return lambda: audio.update(blocked=before)

    with pytest.raises(RuntimeError, match="disable"):
        set_service_enabled(False, run=run, restore_audio=restore_audio)

    assert service == {"enabled": True, "running": True}
    assert audio == {"blocked": ["hdmi"]}


def test_service_and_web_transactions_hold_installer_lifecycle_lock(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    service = {"enabled": False, "running": False}

    def assert_locked() -> None:
        probe = (
            "import fcntl,os,sys;"
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        result = subprocess.run([sys.executable, "-c", probe, str(tmp_path / ".control-freak.lock")], check=False)
        assert result.returncode != 0

    def run(command, **_kwargs):
        action = command[1]
        if action == "list":
            return SimpleNamespace(returncode=0, stdout=f"control_freak;{'*' if service['enabled'] else '-'}\n")
        if action == "status":
            return SimpleNamespace(returncode=0 if service["running"] else 1)
        assert_locked()
        if action == "enable":
            service["enabled"] = True
        elif action == "start":
            service["running"] = True
        return SimpleNamespace(returncode=0)

    assert set_service_enabled(True, run=run, lifecycle_path=path)
    state = configure_web(True, False, state_path=path, run=run, health=lambda: assert_locked() or True)
    assert state["running"] is True


def test_failed_start_rolls_back_enabled_state_and_reports_error(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        configure_web(True, False, state_path=path, run=run, health=lambda: False)

    assert read_state(path)["enabled"] is False
    assert commands == [
        ["/usr/bin/batocera-services", "enable", "control_freak"],
        ["/usr/bin/batocera-services", "start", "control_freak"],
        ["/usr/bin/batocera-services", "stop", "control_freak"],
        ["/usr/bin/batocera-services", "disable", "control_freak"],
    ]


def test_missing_service_command_rolls_back_and_reports_error(tmp_path: Path) -> None:
    path = tmp_path / "web.json"

    def run(_command, **_kwargs):
        raise FileNotFoundError

    with pytest.raises(RuntimeError, match="did not become healthy"):
        configure_web(True, False, state_path=path, run=run, health=lambda: False)

    assert read_state(path)["enabled"] is False


def test_enabled_state_recovers_a_stopped_service(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    update_state(path, enabled=True, tailscale=False)
    commands = []
    health_results = iter((False, True))

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    state = configure_web(True, False, state_path=path, run=run, health=lambda: next(health_results))

    assert state["enabled"] is True
    assert state["running"] is True
    assert commands == [["/usr/bin/batocera-services", "start", "control_freak"]]


def test_failed_restart_restores_previously_stopped_service(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    update_state(path, enabled=True, tailscale=False)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        configure_web(True, False, state_path=path, run=run, health=lambda: False)

    assert read_state(path)["enabled"] is True
    assert commands == [
        ["/usr/bin/batocera-services", "start", "control_freak"],
        ["/usr/bin/batocera-services", "stop", "control_freak"],
    ]


def test_concurrent_configuration_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    health_started = Event()
    release_health = Event()
    results = []

    def first_health() -> bool:
        health_started.set()
        assert release_health.wait(timeout=2)
        return True

    run = lambda command, **_kwargs: SimpleNamespace(returncode=0)
    first = Thread(target=lambda: results.append(configure_web(True, False, state_path=path, run=run, health=first_health)))
    second = Thread(target=lambda: results.append(configure_web(True, False, state_path=path, run=run, health=lambda: True)))
    first.start()
    assert health_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert results == []
    release_health.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(results) == 2
    assert results[0]["pairing_token"] != results[1]["pairing_token"]
    assert all(state["enabled"] and state["running"] for state in results)
