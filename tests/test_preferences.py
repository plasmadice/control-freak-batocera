from pathlib import Path

from control_freak.preferences import read_state, update_state


def test_update_preferences_default_to_stable_daily_checks(tmp_path: Path) -> None:
    assert read_state(tmp_path / "preferences.json") == {
        "version": 1,
        "update_channel": "stable",
        "check_updates_daily": True,
    }


def test_update_preferences_persist_without_command_line(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    assert update_state(path, update_channel="nightly", check_updates_daily=False) == {
        "version": 1,
        "update_channel": "nightly",
        "check_updates_daily": False,
    }
    assert read_state(path)["update_channel"] == "nightly"


def test_invalid_update_preferences_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text('{"version":1,"update_channel":"unsafe","check_updates_daily":true}')

    try:
        read_state(path)
    except ValueError as exc:
        assert str(exc) == "Invalid Control Freak preferences"
    else:
        raise AssertionError("invalid preferences were accepted")
