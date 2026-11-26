from __future__ import annotations

from pathlib import Path
import os
import ssl
import subprocess
import time
from urllib.request import urlopen

from .stateio import locked, write_json
from .web import STATE_PATH, _read_state, _update_state

SERVICE = "control_freak"
LIFECYCLE_LOCK = ".control-freak.lock"


def _service_kwargs(lock_fd: int | None) -> dict:
    if lock_fd is None:
        return {}
    environment = os.environ.copy()
    environment["CONTROL_FREAK_LIFECYCLE_FD"] = str(lock_fd)
    return {"env": environment, "pass_fds": (lock_fd,)}


def read_service_enabled(*, run=subprocess.run, _lock_fd: int | None = None) -> bool:
    try:
        result = run(["/usr/bin/batocera-services", "list", "user"], check=False, capture_output=True, text=True, **_service_kwargs(_lock_fd))
    except OSError:
        return False
    if result.returncode != 0:
        return False
    states = [line.split(";", 1)[1] for line in result.stdout.splitlines() if line.split(";", 1)[0] == SERVICE and ";" in line]
    return len(states) == 1 and states[0].strip() == "*"


def _service_state(run, lock_fd: int | None = None) -> tuple[bool, bool]:
    enabled = read_service_enabled(run=run, _lock_fd=lock_fd)
    try:
        running = run(["/usr/bin/batocera-services", "status", SERVICE], check=False, **_service_kwargs(lock_fd)).returncode == 0
    except OSError:
        running = False
    return enabled, running


def set_service_enabled(enabled: bool, *, run=subprocess.run, restore_audio=None, lifecycle_path: Path | None = None) -> bool:
    path = STATE_PATH if lifecycle_path is None else lifecycle_path
    lifecycle = locked(path, lock_name=LIFECYCLE_LOCK, yield_lock=True)
    with lifecycle as held:
        assert len(held) == 3
        _directory_fd, _name, lock_fd = held
        return _set_service_enabled_serialized(enabled, run=run, restore_audio=restore_audio, lifecycle_path=path, lock_fd=lock_fd)


def _set_service_enabled_serialized(enabled: bool, *, run, restore_audio, lifecycle_path: Path, lock_fd: int) -> bool:
    def invoke(action: str) -> bool:
        try:
            return run(["/usr/bin/batocera-services", action, SERVICE], check=False, **_service_kwargs(lock_fd)).returncode == 0
        except OSError:
            return False

    before_enabled, before_running = _service_state(run, lock_fd)
    compensate_audio = None
    failed_action = "update"
    restore_operation = restore_audio
    if not enabled and restore_operation is None:
        from .audio import read_state, set_blocked
        audio_path = lifecycle_path.with_name("audio.json")
        blocked = read_state(audio_path)["blocked"]

        def restore_operation():
            set_blocked([], audio_path, strict_restore=True)
            return lambda: set_blocked(blocked, audio_path)
    actions = (
        tuple(action for action, needed in (("enable", not before_enabled), ("start", not before_running)) if needed)
        if enabled
        else tuple(action for action, needed in (("stop", before_running),) if needed)
    )
    try:
        for action in actions:
            failed_action = action
            if not invoke(action):
                raise RuntimeError(f"Could not {action} the Control Freak Batocera service")
        if not enabled:
            if _service_state(run, lock_fd)[1]:
                failed_action = "stop"
                raise RuntimeError("Could not stop the Control Freak Batocera service")
            compensate_audio = restore_operation()
            if before_enabled:
                failed_action = "disable"
                if not invoke("disable"):
                    raise RuntimeError("Could not disable the Control Freak Batocera service")
        if _service_state(run, lock_fd) != ((True, True) if enabled else (False, False)):
            raise RuntimeError(f"Could not {failed_action} the Control Freak Batocera service")
    except BaseException as exc:
        compensation_errors = []
        if callable(compensate_audio):
            try:
                compensate_audio()
            except BaseException as compensation_error:
                compensation_errors.append(compensation_error)
        current_enabled, current_running = _service_state(run, lock_fd)
        for restore, needed in (
            ("enable", before_enabled and not current_enabled),
            ("stop", not before_running and current_running),
            ("start", before_running and not current_running),
            ("disable", not before_enabled and current_enabled),
        ):
            if needed:
                if not invoke(restore):
                    compensation_errors.append(RuntimeError(f"Could not compensate service action {restore}"))
        if _service_state(run, lock_fd) != (before_enabled, before_running):
            compensation_errors.append(RuntimeError("Service compensation did not restore prior state"))
        if compensation_errors:
            raise RuntimeError("Could not restore prior service state and audio policy") from exc
        raise
    return enabled


def web_health(*, open_url=urlopen) -> bool:
    deadline = time.monotonic() + 3
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    while time.monotonic() < deadline:
        try:
            with open_url("https://127.0.0.1:8787/health", timeout=0.3, context=context) as response:
                return response.status == 200
        except OSError:
            time.sleep(0.1)
    return False


def configure_web(
    enabled: bool,
    tailscale: bool,
    *,
    state_path: Path = STATE_PATH,
    run=subprocess.run,
    health=web_health,
) -> dict:
    lifecycle = locked(state_path, lock_name=LIFECYCLE_LOCK, yield_lock=True)
    with lifecycle as held:
        assert len(held) == 3
        _directory_fd, _name, lock_fd = held
        with locked(state_path, lock_name="web-config.lock"):
            return _configure_web_serialized(enabled, tailscale, state_path=state_path, run=run, health=health, lock_fd=lock_fd)


def _configure_web_serialized(
    enabled: bool,
    tailscale: bool,
    *,
    state_path: Path = STATE_PATH,
    run=subprocess.run,
    health=web_health,
    lock_fd: int | None = None,
) -> dict:
    with locked(state_path) as (directory_fd, name):
        previous = _read_state(directory_fd, name)
        state = _update_state(directory_fd, name, enabled=enabled, tailscale=tailscale)
        was_running = bool(enabled and previous["enabled"] and health())
        actions = (
            ("enable", "start") if enabled and not previous["enabled"]
            else ("start",) if enabled and not was_running
            else ()
        )

    def invoke(action: str) -> bool:
        try:
            return run(["/usr/bin/batocera-services", action, SERVICE], check=False, **_service_kwargs(lock_fd)).returncode == 0
        except OSError:
            return False

    succeeded = all(invoke(action) for action in actions)
    running = bool(enabled and succeeded and (was_running or health()))
    if enabled and not running:
        if not previous["enabled"]:
            invoke("stop")
            invoke("disable")
        elif not was_running:
            invoke("stop")
        with locked(state_path) as (directory_fd, name):
            persisted_state = {key: value for key, value in state.items() if key != "pairing_token"}
            if _read_state(directory_fd, name) == persisted_state:
                write_json(directory_fd, name, previous)
        raise RuntimeError("The Web app did not become healthy")
    return {**state, "running": running}
