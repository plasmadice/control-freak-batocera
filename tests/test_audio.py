import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from control_freak.audio import enforce, outputs, read_state, set_blocked, set_sink_blocked


def runner(responses, commands):
    def run(command, **_kwargs):
        commands.append(command)
        stdout, returncode = responses.get(tuple(command), ("", 0))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return run


def test_audio_policy_mutes_blocked_sink_and_restores_removed_sink(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    commands = []
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("auto\tauto\nanalog\tAnalog Stereo\nhdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\tanalog\tPipeWire\n2\thdmi\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "analog"): ("Mute: no\n", 0),
    }, commands)

    first = set_blocked(["analog"], path, run)
    second = set_blocked([], path, run)

    assert first["muted"] == ["analog"]
    assert second["restored"] == ["analog"]
    assert ["/usr/bin/pactl", "set-sink-mute", "analog", "1"] in commands
    assert ["/usr/bin/pactl", "set-sink-mute", "analog", "0"] in commands
    assert read_state(path)["blocked"] == []


def test_audio_policy_rejects_unknown_output(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    run = runner({("/usr/bin/batocera-audio", "list"): ("auto\tauto\nhdmi\tHDMI\n", 0)}, [])

    with pytest.raises(ValueError, match="no longer available"):
        set_blocked(["missing"], path, run)


def test_audio_state_rejects_symlinked_parent(tmp_path: Path) -> None:
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    parent = tmp_path / "control-freak"
    parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(OSError):
        set_blocked([], parent / "audio.json", runner({("/usr/bin/batocera-audio", "list"): ("", 0)}, []))

    assert not (redirected / "audio.json").exists()


def test_audio_outputs_ignore_auto_and_preserve_labels() -> None:
    run = runner({("/usr/bin/batocera-audio", "list"): ("auto\tauto\nanalog\tBuilt-in Analog Stereo\n", 0)}, [])

    assert outputs(run) == [{"value": "analog", "label": "Built-in Analog Stereo"}]


def test_malformed_audio_policy_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"blocked":"analog","managed":[],"version":1}')

    with pytest.raises(ValueError, match="Invalid audio policy state"):
        enforce(path, runner({}, []))


def test_unavailable_or_failed_sink_is_not_marked_managed(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\thdmi\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "hdmi"): ("Mute: no\n", 0),
        ("/usr/bin/pactl", "set-sink-mute", "hdmi", "1"): ("", 1),
    }, [])

    state = set_blocked(["hdmi"], path, run)

    assert state["blocked"] == ["hdmi"]
    assert state["managed"] == []
    assert state["muted"] == []


def test_single_sink_update_preserves_other_selections(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("analog\tAnalog\nhdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("", 0),
    }, [])
    set_blocked(["analog"], path, run)

    state = set_sink_blocked("hdmi", True, path, run)

    assert state["blocked"] == ["analog", "hdmi"]
    assert state["managed"] == []


@pytest.mark.parametrize("single", [False, True])
def test_failed_enforcement_restores_prior_policy(tmp_path: Path, single: bool) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":["analog"],"managed":[]}')

    def run(command, **_kwargs):
        if command[:2] == ["/usr/bin/batocera-audio", "list"]:
            return SimpleNamespace(stdout="analog\tAnalog\nhdmi\tHDMI\n", returncode=0)
        raise subprocess.TimeoutExpired(command, 10)

    with pytest.raises(subprocess.TimeoutExpired):
        if single:
            set_sink_blocked("hdmi", True, path, run)
        else:
            set_blocked(["hdmi"], path, run)

    assert read_state(path) == {"version": 1, "blocked": ["analog"], "managed": []}


def test_failed_mute_keeps_ownership_until_physical_rollback(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":[],"managed":[]}')
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["/usr/bin/batocera-audio", "list"]:
            return SimpleNamespace(stdout="hdmi\tHDMI\n", returncode=0)
        if command[:4] == ["/usr/bin/pactl", "list", "short", "sinks"]:
            return SimpleNamespace(stdout="1\thdmi\tPipeWire\n", returncode=0)
        if command[:2] == ["/usr/bin/pactl", "get-sink-mute"]:
            return SimpleNamespace(stdout="Mute: no\n", returncode=0)
        if command == ["/usr/bin/pactl", "set-sink-mute", "hdmi", "1"]:
            raise subprocess.TimeoutExpired(command, 10)
        return SimpleNamespace(stdout="", returncode=0)

    with pytest.raises(subprocess.TimeoutExpired):
        set_blocked(["hdmi"], path, run)

    assert ["/usr/bin/pactl", "set-sink-mute", "hdmi", "0"] in commands
    assert read_state(path) == {"version": 1, "blocked": [], "managed": []}


def test_previously_muted_sink_is_never_claimed_or_unmuted(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    commands = []
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\thdmi\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "hdmi"): ("Mute: yes\n", 0),
    }, commands)

    assert set_blocked(["hdmi"], path, run)["managed"] == []
    set_blocked([], path, run)

    assert ["/usr/bin/pactl", "set-sink-mute", "hdmi", "0"] not in commands


def test_unmanaged_sink_is_not_muted_when_prior_state_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    commands = []
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\thdmi\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "hdmi"): ("", 1),
    }, commands)

    state = set_blocked(["hdmi"], path, run)

    assert state["managed"] == []
    assert ["/usr/bin/pactl", "set-sink-mute", "hdmi", "1"] not in commands


def test_strict_restore_reports_failed_owned_unmute(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":["hdmi"],"managed":["hdmi"]}')
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\thdmi\tPipeWire\n", 0),
        ("/usr/bin/pactl", "set-sink-mute", "hdmi", "0"): ("", 1),
    }, [])

    with pytest.raises(RuntimeError, match="Unable to restore"):
        set_blocked([], path, run, strict_restore=True)

    assert read_state(path)["managed"] == ["hdmi"]
    assert read_state(path)["blocked"] == ["hdmi"]


def test_strict_restore_fails_when_sink_discovery_fails(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":["hdmi"],"managed":["hdmi"]}')
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("", 1),
    }, [])

    with pytest.raises(RuntimeError, match="Unable to restore"):
        set_blocked([], path, run, strict_restore=True)


@pytest.mark.parametrize("output,code", [("", 0), ("", 1)])
def test_strict_restore_fails_when_owned_sink_is_not_discovered(tmp_path: Path, output: str, code: int) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":["hdmi"],"managed":["hdmi"]}')
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("hdmi\tHDMI\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): (output, code),
    }, [])

    with pytest.raises(RuntimeError, match="Unable to restore"):
        set_blocked([], path, run, strict_restore=True)


def test_new_mute_ownership_is_durable_before_pactl_mutation(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":1,"blocked":[],"managed":[]}')

    def run(command, **_kwargs):
        if command[:2] == ["/usr/bin/batocera-audio", "list"]:
            return SimpleNamespace(stdout="hdmi\tHDMI\n", returncode=0)
        if command[:4] == ["/usr/bin/pactl", "list", "short", "sinks"]:
            return SimpleNamespace(stdout="1\thdmi\tPipeWire\n", returncode=0)
        if command[:2] == ["/usr/bin/pactl", "get-sink-mute"]:
            return SimpleNamespace(stdout="Mute: no\n", returncode=0)
        if command[:2] == ["/usr/bin/pactl", "set-sink-mute"]:
            assert json.loads(path.read_text())["managed"] == ["hdmi"]
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout="", returncode=1)

    set_blocked(["hdmi"], path, run)


def test_rk3326_speaker_value_controls_internal_sink(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    commands = []
    run = runner({
        ("/usr/bin/batocera-audio", "list"): ("speakers\tSpeakers\n", 0),
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\tINTERNAL\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "INTERNAL"): ("Mute: no\n", 0),
        ("/usr/bin/pactl", "set-sink-mute", "INTERNAL", "1"): ("", 0),
    }, commands)

    state = set_blocked(["speakers"], path, run)

    assert state["managed"] == ["INTERNAL"]
    assert ["/usr/bin/pactl", "set-sink-mute", "INTERNAL", "1"] in commands


def test_audio_state_rejects_boolean_version(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    path.write_text('{"version":true,"blocked":[],"managed":[]}')

    with pytest.raises(ValueError, match="Invalid audio policy"):
        read_state(path)


def test_audio_policy_rejects_more_than_64_outputs_before_write(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    values = [f"sink-{index}" for index in range(65)]
    listing = "".join(f"{value}\t{value}\n" for value in values)

    with pytest.raises(ValueError, match="Invalid audio output selection"):
        set_blocked(values, path, runner({("/usr/bin/batocera-audio", "list"): (listing, 0)}, []))

    assert not path.exists()


def test_enforcement_never_overflows_managed_ownership(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    managed = [f"old-{index}" for index in range(64)]
    path.write_text(json.dumps({"version": 1, "blocked": ["new"], "managed": managed}))
    commands = []
    run = runner({
        ("/usr/bin/pactl", "list", "short", "sinks"): ("1\tnew\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", "new"): ("Mute: no\n", 0),
    }, commands)

    state = enforce(path, run)

    assert state["managed"] == managed
    assert ["/usr/bin/pactl", "set-sink-mute", "new", "1"] not in commands


def test_full_managed_set_still_reapplies_owned_mute(tmp_path: Path) -> None:
    path = tmp_path / "audio.json"
    managed = [f"sink-{index}" for index in range(64)]
    path.write_text(json.dumps({"version": 1, "blocked": [managed[0]], "managed": managed}))
    commands = []
    run = runner({
        ("/usr/bin/pactl", "list", "short", "sinks"): (f"1\t{managed[0]}\tPipeWire\n", 0),
        ("/usr/bin/pactl", "get-sink-mute", managed[0]): ("Mute: no\n", 0),
    }, commands)

    state = enforce(path, run)

    assert state["managed"] == managed
    assert ["/usr/bin/pactl", "set-sink-mute", managed[0], "1"] in commands
