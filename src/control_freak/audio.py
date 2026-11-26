from __future__ import annotations

from pathlib import Path
import subprocess

from .stateio import locked, read_json, write_json

STATE_PATH = Path("/userdata/system/control-freak/audio.json")


def _valid_items(items) -> bool:
    return (
        isinstance(items, list)
        and len(items) <= 64
        and all(isinstance(item, str) and item and len(item) <= 512 and "\0" not in item and "\n" not in item for item in items)
    )


def _read_state(directory_fd: int, name: str) -> dict:
    try:
        value = read_json(directory_fd, name)
    except FileNotFoundError:
        return {"version": 1, "blocked": [], "managed": []}
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "blocked", "managed"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or any(not _valid_items(items) for items in (value.get("blocked"), value.get("managed")))
    ):
        raise ValueError("Invalid audio policy state")
    return value


def read_state(path: Path = STATE_PATH) -> dict:
    with locked(path) as (directory_fd, name):
        return _read_state(directory_fd, name)


def outputs(run=subprocess.run) -> list[dict[str, str]]:
    result = run(["/usr/bin/batocera-audio", "list"], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        return []
    found = []
    for line in result.stdout.splitlines():
        value, separator, label = line.partition("\t")
        if separator and value and value != "auto" and len(value) <= 512 and "\0" not in value:
            found.append({"value": value, "label": label.strip() or value})
    return found


def _pipewire_sink(value: str) -> str:
    return "INTERNAL" if value in {"speakers", "headphone"} else value


def _enforce(directory_fd: int, name: str, run) -> dict:
    state = _read_state(directory_fd, name)
    result = run(["/usr/bin/pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=10, check=False)
    discovery_failed = result.returncode != 0
    live = {fields[1] for line in result.stdout.splitlines() if len(fields := line.split()) >= 2} if not discovery_failed else set()
    blocked_targets = {_pipewire_sink(sink) for sink in state["blocked"]}
    restored = []
    restore_failed = [sink for sink in state["managed"] if sink not in blocked_targets and (discovery_failed or sink not in live)]
    muted = []
    for sink in state["managed"]:
        if sink in live and sink not in blocked_targets:
            if run(["/usr/bin/pactl", "set-sink-mute", sink, "0"], timeout=10, check=False).returncode == 0:
                restored.append(sink)
            else:
                restore_failed.append(sink)
    managed = [sink for sink in state["managed"] if sink not in restored]
    for sink in state["blocked"]:
        target = _pipewire_sink(sink)
        if target in live:
            prior = run(["/usr/bin/pactl", "get-sink-mute", target], capture_output=True, text=True, timeout=10, check=False)
            was_muted = prior.returncode == 0 and prior.stdout.strip().lower() == "mute: yes"
            was_unmuted = prior.returncode == 0 and prior.stdout.strip().lower() == "mute: no"
            if was_muted:
                muted.append(sink)
                continue
            newly_owned = target not in managed and was_unmuted and len(managed) < 64
            if newly_owned:
                managed.append(target)
                state = {**state, "managed": managed}
                write_json(directory_fd, name, state)
            if target in managed:
                if run(["/usr/bin/pactl", "set-sink-mute", target, "1"], timeout=10, check=False).returncode == 0:
                    muted.append(sink)
                elif newly_owned:
                    managed.remove(target)
                    state = {**state, "managed": managed}
                    write_json(directory_fd, name, state)
    if state["managed"] != managed:
        state = {**state, "managed": managed}
        write_json(directory_fd, name, state)
    return {**state, "muted": muted, "restored": restored, "restore_failed": restore_failed}


def enforce(path: Path = STATE_PATH, run=subprocess.run) -> dict:
    with locked(path) as (directory_fd, name):
        return _enforce(directory_fd, name, run)


def _rollback_change(directory_fd: int, name: str, current: dict, run) -> None:
    failed = _read_state(directory_fd, name)
    write_json(directory_fd, name, {**current, "managed": failed["managed"]})
    try:
        _enforce(directory_fd, name, run)
    except BaseException:
        pass


def set_blocked(blocked: list[str], path: Path = STATE_PATH, run=subprocess.run, *, strict_restore: bool = False) -> dict:
    if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
        raise ValueError("Audio outputs must be strings")
    selected = list(dict.fromkeys(item for item in blocked if item and item != "auto"))
    if not _valid_items(selected):
        raise ValueError("Invalid audio output selection")
    with locked(path) as (directory_fd, name):
        current = _read_state(directory_fd, name)
        allowed = {item["value"] for item in outputs(run)} | set(current["blocked"])
        if set(selected) - allowed:
            raise ValueError("One or more audio outputs are no longer available")
        write_json(directory_fd, name, {"version": 1, "blocked": selected, "managed": current["managed"]})
        try:
            state = _enforce(directory_fd, name, run)
        except BaseException:
            _rollback_change(directory_fd, name, current, run)
            raise
        if strict_restore and state["restore_failed"]:
            write_json(directory_fd, name, current)
            _enforce(directory_fd, name, run)
            raise RuntimeError("Unable to restore one or more audio outputs")
        return state


def set_sink_blocked(sink: str, blocked: bool, path: Path = STATE_PATH, run=subprocess.run) -> dict:
    if not isinstance(sink, str) or not sink or type(blocked) is not bool:
        raise ValueError("Invalid audio output update")
    with locked(path) as (directory_fd, name):
        current = _read_state(directory_fd, name)
        if sink not in {item["value"] for item in outputs(run)} | set(current["blocked"]):
            raise ValueError("Audio output is no longer available")
        selected = [value for value in current["blocked"] if value != sink]
        if blocked:
            selected.append(sink)
        if not _valid_items(selected):
            raise ValueError("Invalid audio output selection")
        write_json(directory_fd, name, {**current, "blocked": selected})
        try:
            return _enforce(directory_fd, name, run)
        except BaseException:
            _rollback_change(directory_fd, name, current, run)
            raise
