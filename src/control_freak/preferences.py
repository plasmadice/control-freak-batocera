from __future__ import annotations

import json
from pathlib import Path

from .stateio import locked, opened, read_json, write_json

DEFAULT = {"version": 1, "update_channel": "stable", "check_updates_daily": True}


def _validate(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != set(DEFAULT)
        or value.get("version") != 1
        or value.get("update_channel") not in {"stable", "nightly"}
        or type(value.get("check_updates_daily")) is not bool
    ):
        raise ValueError("Invalid Control Freak preferences")
    return value


def read_state(path: Path = Path("/userdata/system/control-freak/preferences.json")) -> dict:
    try:
        with opened(path) as (directory_fd, name):
            return _validate(read_json(directory_fd, name))
    except FileNotFoundError:
        return dict(DEFAULT)


def update_state(
    path: Path = Path("/userdata/system/control-freak/preferences.json"),
    *,
    update_channel: str,
    check_updates_daily: bool,
) -> dict:
    value = _validate(
        {
            "version": 1,
            "update_channel": update_channel,
            "check_updates_daily": check_updates_daily,
        }
    )
    with locked(path) as (directory_fd, name):
        write_json(directory_fd, name, value)
    return value
