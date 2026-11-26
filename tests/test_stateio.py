from pathlib import Path
import threading

import pytest

import control_freak.stateio as stateio
from control_freak.stateio import locked


def test_audio_and_web_state_share_one_lock(tmp_path: Path) -> None:
    acquired = threading.Event()

    def lock_audio() -> None:
        with locked(tmp_path / "audio.json"):
            acquired.set()

    with locked(tmp_path / "web.json"):
        thread = threading.Thread(target=lock_audio)
        thread.start()
        assert not acquired.wait(0.1)

    thread.join(timeout=1)
    assert acquired.is_set()


def test_runtime_lockout_refuses_state_mutation(tmp_path: Path) -> None:
    (tmp_path / "runtime.lockout").write_text('{"version":1}\n')

    with pytest.raises(RuntimeError, match="uninstalled"):
        with locked(tmp_path / "audio.json"):
            pass


def test_state_access_rejects_world_writable_directory(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)

    with pytest.raises(RuntimeError, match="unsafe Control Freak state directory"):
        with locked(tmp_path / "audio.json"):
            pass


def test_state_access_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stateio.os, "geteuid", lambda: tmp_path.stat().st_uid + 1)

    with pytest.raises(RuntimeError, match="unsafe Control Freak state directory"):
        with locked(tmp_path / "audio.json"):
            pass
