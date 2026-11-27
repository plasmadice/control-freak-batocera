import json
import hashlib
import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest

import deploy.install_batocera as installer
from deploy.install_batocera import install, rollback, rollback_info, uninstall


@pytest.fixture(autouse=True)
def batocera_os_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path.parent / f"{tmp_path.name}-os-release"
    path.write_text('NAME=Batocera.linux\nID=buildroot\nVERSION="43.1"\nVERSION_ID=2024.11\n')
    monkeypatch.setattr(installer, "OS_RELEASE_PATH", path)
    return path


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    source = tmp_path.parent / f"{tmp_path.name}-signed-source"
    shutil.copytree(Path(__file__).resolve().parents[1], source, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))
    resign_source(source)
    return source


@pytest.mark.parametrize("contents", [
    'NAME=Batocera.linux\nID=buildroot\n',
    'NAME=Batocera.linux\nID=buildroot\nVERSION=42\n',
    'NAME=Batocera.linux\nID=buildroot\nVERSION=45\n',
    'NAME=Batocera.linux\nID=buildroot\nVERSION=43beta\n',
    'NAME=Other\nID=buildroot\nVERSION=43\n',
    'NAME=Batocera.linux\nID=other\nVERSION=43\n',
    'NAME=Batocera.linux\nID=buildroot\nVERSION=43\nVERSION=44\n',
    'NAME =Batocera.linux\nID=buildroot\nVERSION=43\n',
])
def test_install_rejects_unsupported_or_malformed_os_release_before_mutation(
    tmp_path: Path, source_root: Path, batocera_os_release: Path, contents: str,
) -> None:
    batocera_os_release.write_text(contents)

    with pytest.raises(RuntimeError, match="Batocera 43 or 44"):
        install(tmp_path / "userdata", source_root, "1.0.0", "stable")

    assert not (tmp_path / "userdata").exists()


@pytest.mark.parametrize("version", ["43", "43.1", "44", "44.2.3"])
def test_install_accepts_supported_batocera_major(
    tmp_path: Path, source_root: Path, batocera_os_release: Path, version: str,
) -> None:
    batocera_os_release.write_text(
        f'NAME="Batocera.linux"\nID=buildroot\nVERSION="{version}"\nVERSION_ID=2026.05-git\n'
    )

    assert install(tmp_path / "userdata", source_root, "1.0.0", "stable")


def app(root: Path) -> Path:
    return root / "system/control-freak/app"


def current(root: Path) -> dict:
    return json.loads((app(root) / "current.json").read_text())


def registered(root: Path) -> bool:
    path = root / "system/control-freak/library/gamelist.xml"
    if not path.exists():
        return False
    games = ET.parse(path).getroot().findall("game")
    return any(game.findtext("path") == installer.GAME_PATH for game in games)


def copied_source(tmp_path: Path, source_root: Path) -> Path:
    target = tmp_path / "source"
    shutil.copytree(source_root, target, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))
    return target


def test_verified_predecessor_can_be_rolled_back_through_activation_journal(tmp_path: Path, source_root: Path) -> None:
    private = resign_source(source_root)
    install(tmp_path, source_root, "1.0.0", "stable")
    second = copied_source(tmp_path / "second", source_root)
    (second / "src/control_freak/__init__.py").write_text("SECOND = True\n")
    resign_source(second, private)
    install(tmp_path, second, "1.1.0", "stable")

    assert rollback_info(tmp_path) == "1.0.0"
    assert rollback(tmp_path) == ["activate release 1.0.0"]
    assert current(tmp_path) == {"version": 1, "release_id": "1.0.0", "channel": "stable"}
    assert rollback_info(tmp_path) == "1.1.0"


def test_launcher_boundary_requires_owned_nonwritable_app_ancestors(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "1.0.0", "stable")
    app(tmp_path).chmod(0o777)

    with pytest.raises(RuntimeError, match="unsafe launcher trust boundary"):
        install(tmp_path, source_root, "1.0.1", "stable")


def make_legacy_v3(root: Path) -> Path:
    application = root / installer.LEGACY_APP_REL
    application.parent.mkdir(parents=True, exist_ok=True)
    app(root).rename(application)
    launcher = application / installer.LEGACY_LAUNCHER
    launcher.write_text("#!/usr/bin/python3\n")
    launcher.chmod(0o755)
    manifest_path = application / installer.ROOT_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 3
    manifest.pop("registration")
    manifest["static"][installer.LEGACY_LAUNCHER] = installer.checksum(launcher)
    manifest_path.write_bytes(installer._json_bytes(manifest))
    return launcher


def resign_source(source: Path, private: bytes | None = None) -> bytes:
    if private is None:
        private = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
            capture_output=True,
            check=True,
        ).stdout
    public = subprocess.run(["openssl", "pkey", "-pubout"], input=private, capture_output=True, check=True).stdout
    installer.RELEASE_PUBLIC_KEY = public
    (source / "deploy/release-public.pem").write_bytes(public)
    files = []
    package = source / "src/control_freak"
    for path in sorted(package.rglob("*")):
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts:
            files.append({
                "path": (Path("control_freak") / path.relative_to(package)).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": 0o644,
            })
    service = installer._service_bytes(source, Path("/userdata"))
    source_files = []
    for relative, mode in installer.TRUSTED_SOURCE_FILES.items():
        payload = (source / relative).read_bytes()
        source_files.append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "mode": mode})
    statement = (json.dumps({
        "version": 2,
        "files": files,
        "service": {"sha256": hashlib.sha256(service).hexdigest(), "mode": installer.SERVICE_MODE},
        "source": source_files,
    }, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.memfd_create("test-installer-signing-key")
    try:
        os.write(descriptor, private)
        os.lseek(descriptor, 0, os.SEEK_SET)
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f"/proc/self/fd/{descriptor}"],
            input=statement,
            pass_fds=(descriptor,),
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.close(descriptor)
    (source / "deploy/release-trust.json").write_bytes(statement)
    (source / "deploy/release-trust.sig").write_bytes(signature)
    return private


def test_live_install_service_actions_use_batocera_service_manager(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0))

    installer._service_actions(Path("/userdata"), "enable", "stop", "start")

    assert commands == [
        ["/usr/bin/batocera-services", "enable", "control_freak"],
        ["/usr/bin/batocera-services", "stop", "control_freak"],
        ["/usr/bin/batocera-services", "start", "control_freak"],
    ]


def test_live_service_status_inherits_lifecycle_lock(monkeypatch) -> None:
    calls = []
    real_fstat = os.fstat
    real_stat = os.stat
    lock_path = Path("/userdata") / installer.APP_PARENT_REL / installer.LOCK_FILE
    monkeypatch.setattr(installer, "_ACTIVE_LIFECYCLE_LOCK", 99)
    monkeypatch.setattr(installer.os, "fstat", lambda fd: SimpleNamespace(st_dev=1, st_ino=2) if fd == 99 else real_fstat(fd))
    monkeypatch.setattr(
        installer.os,
        "stat",
        lambda path, *args, **kwargs: SimpleNamespace(st_dev=1, st_ino=2) if Path(path) == lock_path else real_stat(path, *args, **kwargs),
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )

    assert installer._service_running(Path("/userdata")) is True
    assert calls[0][0] == ["/usr/bin/batocera-services", "status", "control_freak"]
    assert calls[0][1]["pass_fds"] == (99,)
    assert calls[0][1]["env"]["CONTROL_FREAK_LIFECYCLE_FD"] == "99"


@pytest.mark.parametrize(
    "returncode, stdout, stderr",
    [
        (0, "unexpected output\n", ""),
        (1, "", "Invalid Control Freak PID evidence\n"),
    ],
)
def test_live_service_status_rejects_ambiguous_result(
    monkeypatch, returncode: int, stdout: str, stderr: str
) -> None:
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, returncode, stdout, stderr
        ),
    )

    with pytest.raises(RuntimeError, match="determine Control Freak service state"):
        installer._service_running(Path("/userdata"))


@pytest.mark.parametrize("marker, expected", [("*", True), ("-", False)])
def test_live_service_state_parses_semicolon_listing(monkeypatch, marker: str, expected: bool) -> None:
    def run(command, **_kwargs):
        if command[1:3] == ["list", "user"]:
            return subprocess.CompletedProcess(command, 0, f"other;*\ncontrol_freak;{marker}\n", "")
        if command[1] == "status":
            return subprocess.CompletedProcess(command, 1)
        raise AssertionError(command)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer._service_enabled(Path("/userdata")) is expected


def test_service_canonical_bytes_equal_frozen_userdata_install(source_root: Path) -> None:
    assert installer._service_bytes(source_root, Path("/userdata")) == (
        source_root / "deploy/control_freak"
    ).read_bytes().replace(
        b'PUBLIC_KEY_PEM = b"__CONTROL_FREAK_RELEASE_PUBLIC_KEY__"',
        f"PUBLIC_KEY_PEM = {(source_root / 'deploy/release-public.pem').read_bytes()!r}".encode(),
    )


def test_live_uninstall_clears_runtime_state_with_installed_release(monkeypatch) -> None:
    calls = []
    writes = []
    service = {"enabled": True, "running": True}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "stop":
            service["running"] = False
        elif command[1] == "disable":
            service["enabled"] = False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *args: writes.append(args))

    installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert calls[0][0] == ["/usr/bin/batocera-services", "stop", "control_freak"]
    assert calls[1][0] == ["/usr/bin/batocera-services", "disable", "control_freak"]
    assert any(write[1] == installer.RUNTIME_LOCKOUT for write in writes)


def test_live_uninstall_accepts_nonzero_stop_after_worker_exits(monkeypatch) -> None:
    actions = []
    service = {"enabled": True, "running": True}

    def action(_root: Path, requested: str) -> None:
        actions.append(requested)
        if requested == "stop":
            service["running"] = False
            raise RuntimeError("Unable to stop Control Freak service")
        service["enabled"] = False

    monkeypatch.setattr(installer, "_service_actions", action)
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)

    installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert actions == ["stop", "disable"]


def test_live_uninstall_releases_web_lock_before_service_action(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = iter((10, 11, 12))
    closed = []
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: False)
    monkeypatch.setattr(installer, "_service_running", lambda _root: False)
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: next(opened))
    monkeypatch.setattr(installer.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)

    def action(_root: Path, requested: str) -> None:
        assert requested == "disable"
        assert 11 in closed

    monkeypatch.setattr(installer, "_service_actions", action)
    installer._shutdown_runtime(Path("/userdata"), "release-one", True)


def test_live_uninstall_rejects_unverified_stop_before_audio_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    actions = []
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: True)
    monkeypatch.setattr(installer, "_service_running", lambda _root: True)
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_service_actions", lambda _root, action: actions.append(action))

    with pytest.raises(RuntimeError, match="stop"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert "disable" not in actions


def test_runtime_lockout_is_durable_and_clearable(tmp_path: Path) -> None:
    state = tmp_path / "system/control-freak"
    state.mkdir(parents=True)

    assert installer._runtime_lockout(tmp_path, True) is False
    assert json.loads((state / installer.RUNTIME_LOCKOUT).read_text()) == {"version": 1}
    assert installer._runtime_lockout(tmp_path, False) is True
    assert not (state / installer.RUNTIME_LOCKOUT).exists()


def test_open_dir_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside/services"
    outside.mkdir(parents=True)
    root = tmp_path / "userdata"
    root.mkdir()
    (root / "system").symlink_to(outside.parent, target_is_directory=True)

    with pytest.raises(OSError):
        installer._open_dir(root / "system/services")




def test_live_uninstall_stops_recorded_process_when_service_file_is_missing(monkeypatch) -> None:
    calls = []
    stopped = []
    service = {"enabled": True, "running": True}

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "disable":
            service.update(enabled=False, running=False)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_stop_runtime_process", lambda root: stopped.append(root))
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)

    installer._shutdown_runtime(Path("/userdata"), "release-one", False)

    assert stopped == [Path("/userdata")]
    assert calls[-1] == ["/usr/bin/batocera-services", "disable", "control_freak"]


def test_live_uninstall_restores_web_state_when_audio_restore_fails(monkeypatch) -> None:
    calls = []
    writes = []
    previous_web = b'{"version":1,"enabled":true,"tailscale":false,"token":"' + b"x" * 43 + b'"}\n'
    previous_audio = b'{"version":1,"blocked":["sink"],"managed":["sink"]}\n'
    service = {"enabled": True, "running": True}

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "stop":
            service["running"] = False
        elif command[1] == "start":
            service["running"] = True
        return subprocess.CompletedProcess(command, 1 if command[:3] == ["/usr/bin/pactl", "set-sink-mute", "sink"] else 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(
        installer,
        "_read_at",
        lambda _fd, name: (_ for _ in ()).throw(FileNotFoundError())
        if name == installer.RUNTIME_LOCKOUT
        else ((previous_web if name == "web.json" else previous_audio), SimpleNamespace(st_mode=0o100600)),
    )
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *args: writes.append(args))
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Unable to restore"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert any(write[2] == previous_web for write in writes)
    assert any(write[2] == previous_audio for write in writes)
    assert ["/usr/bin/batocera-services", "start", "control_freak"] in calls


def test_live_uninstall_restores_runtime_state_when_stop_fails(monkeypatch) -> None:
    calls = []
    writes = []
    previous_web = b'{"version":1,"enabled":true,"tailscale":false,"token":"' + b"x" * 43 + b'"}\n'
    previous_audio = b'{"version":1,"blocked":["sink"],"managed":["sink"]}\n'

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: True)
    monkeypatch.setattr(installer, "_service_running", lambda _root: True)
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(
        installer,
        "_read_at",
        lambda _fd, name: (_ for _ in ()).throw(FileNotFoundError())
        if name == installer.RUNTIME_LOCKOUT
        else ((previous_web if name == "web.json" else previous_audio), SimpleNamespace(st_mode=0o100600)),
    )
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *args: writes.append(args))
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)

    def service_action(_root: Path, action: str) -> None:
        calls.append(action)
        if action == "stop":
            raise RuntimeError("Unable to stop")

    monkeypatch.setattr(installer, "_service_actions", service_action)
    with pytest.raises(RuntimeError, match="Unable to stop"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert writes[-1][2] == previous_web
    assert previous_audio not in [write[2] for write in writes]
    assert calls == ["stop", "enable", "start"]


def test_live_uninstall_reenables_service_when_disable_partially_applies(monkeypatch) -> None:
    service = {"enabled": True, "running": True}
    calls = []

    def action(_root: Path, requested: str) -> None:
        calls.append(requested)
        if requested == "stop":
            service["running"] = False
        elif requested == "disable":
            service["enabled"] = False
            raise RuntimeError("disable reported failure")
        elif requested == "enable":
            service["enabled"] = True
        elif requested == "start":
            service["running"] = True

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(installer, "_service_actions", action)

    with pytest.raises(RuntimeError, match="disable reported failure"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert service == {"enabled": True, "running": True}
    assert calls == ["stop", "disable", "enable", "start"]


def test_live_uninstall_rejects_unverified_disable_and_restores_exact_state(monkeypatch) -> None:
    service = {"enabled": True, "running": True}
    calls = []

    def action(_root: Path, requested: str) -> None:
        calls.append(requested)
        if requested == "stop":
            service["running"] = False
        elif requested == "start":
            service["running"] = True

    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_service_actions", action)

    with pytest.raises(RuntimeError, match="disable"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert service == {"enabled": True, "running": True}
    assert calls == ["stop", "disable", "enable", "start"]


def test_live_uninstall_attempts_start_when_rollback_enable_fails(monkeypatch) -> None:
    calls = []
    removed = []
    service = {"enabled": True, "running": True}

    def action(_root: Path, requested: str) -> None:
        calls.append(requested)
        if requested == "stop":
            service["running"] = False
        if requested in {"disable", "enable"}:
            raise RuntimeError(f"{requested} failed")
        if requested == "start":
            service["running"] = True

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_mode=0o040700))
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: service["running"])
    monkeypatch.setattr(installer, "_prepare_state_directory", lambda _root: None)
    monkeypatch.setattr(installer.os, "open", lambda *args, **kwargs: 99)
    monkeypatch.setattr(installer.os, "close", lambda _fd: None)
    monkeypatch.setattr(installer.fcntl, "flock", lambda *_args: None)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(installer, "_atomic_write_at", lambda *_args: None)
    monkeypatch.setattr(installer.os, "unlink", lambda name, **_kwargs: removed.append(name))
    monkeypatch.setattr(installer, "_service_actions", action)

    with pytest.raises(RuntimeError, match="Unable to activate"):
        installer._shutdown_runtime(Path("/userdata"), "release-one", True)

    assert calls == ["stop", "disable", "enable", "start"]
    assert installer.UNINSTALL_JOURNAL not in removed


def test_live_install_checks_qrcode_runtime_dependency(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0))

    installer._runtime_dependencies(Path("/userdata"))

    assert commands == [[sys.executable, "-B", "-c", "import fcntl, os, qrcode; assert hasattr(os, 'memfd_create') and hasattr(fcntl, 'F_ADD_SEALS')"]]


def test_atomic_create_never_replaces_existing_service(tmp_path: Path) -> None:
    service = tmp_path / "control_freak"
    service.write_bytes(b"unknown")
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="Refusing existing file"):
            installer._atomic_create_at(directory_fd, "control_freak", b"candidate", 0o755)
    finally:
        os.close(directory_fd)

    assert service.read_bytes() == b"unknown"


def test_install_registers_versioned_custom_system_application(tmp_path: Path, source_root: Path) -> None:
    operations = install(tmp_path, source_root, "0.1.0-test", "stable")

    root = app(tmp_path)
    assert (root / "control-freak.pygame").is_file()
    assert json.loads((root / installer.ROOT_MANIFEST).read_text())["version"] == 7
    assert (root / "image.png").is_file()
    assert (root / "releases/0.1.0-test/control_freak/app.py").is_file()
    assert current(tmp_path) == {"channel": "stable", "release_id": "0.1.0-test", "version": 1}
    assert (root / "install_batocera.py").is_file()
    assert "install release 0.1.0-test" in operations
    assert registered(tmp_path)


def test_upgrade_from_version_six_registers_native_pygame(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    manifest_path = application / installer.ROOT_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 6
    manifest["static"].pop("control-freak.pygame")
    (application / "control-freak.pygame").unlink()
    installer._sync_pygame_gamelist(tmp_path, remove=True)
    manifest_path.write_bytes(installer._json_bytes(manifest))

    install(tmp_path, source_root, "two", "stable")

    upgraded = json.loads(manifest_path.read_text())
    assert upgraded["version"] == 7
    assert (application / "control-freak.pygame").is_file()
    games = ET.parse(tmp_path / "roms/pygame/gamelist.xml").getroot().findall("game")
    assert [game.findtext("path") for game in games].count(installer.PYGAME_GAME_PATH) == 1


def test_install_uses_native_pygame_system_without_rom_executables(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")

    application = tmp_path / "system/control-freak/app"
    library = tmp_path / "system/control-freak/library"
    overlay = tmp_path / "system/configs/emulationstation/es_systems_control_freak.cfg"
    assert (application / "releases/one/control_freak/app.py").is_file()
    assert overlay.read_text() == installer.SYSTEMS_OVERLAY
    assert ET.parse(overlay).getroot().find("system") is None
    assert (library / "Control Freak.controlfreak").read_bytes() == b""
    assert (library / "Control Freak.controlfreak").stat().st_mode & 0o111 == 0
    game = ET.parse(library / "gamelist.xml").getroot().find("game")
    assert game is not None
    assert game.findtext("path") == "./Control Freak.controlfreak"
    assert game.findtext("image") == "./images/control-freak.png"
    native_game = ET.parse(tmp_path / "roms/pygame/gamelist.xml").getroot().find("game")
    assert native_game is not None
    assert native_game.findtext("path") == installer.PYGAME_GAME_PATH
    assert native_game.findtext("image") == "../../system/control-freak/app/image.png"
    manifest = json.loads((application / installer.ROOT_MANIFEST).read_text())
    assert manifest["registration"] == {
        "overlay": {"sha256": installer.checksum(overlay), "mode": 0o644},
        "marker": {"sha256": installer.checksum(library / "Control Freak.controlfreak"), "mode": 0o644},
        "gamelist": {"sha256": installer.checksum(library / "gamelist.xml"), "mode": 0o644},
        "image": {"sha256": installer.checksum(library / "images/control-freak.png"), "mode": 0o644},
    }


def test_update_replaces_owned_ports_theme_registration(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    overlay = tmp_path / installer.OVERLAY_REL
    overlay.write_text("<systemList><system /></systemList>\n")
    manifest = json.loads((application / installer.ROOT_MANIFEST).read_text())
    manifest["registration"]["overlay"]["sha256"] = installer.checksum(overlay)
    (application / installer.ROOT_MANIFEST).write_bytes(installer._json_bytes(manifest))

    install(tmp_path, source_root, "two", "stable")

    assert overlay.read_text() == installer.SYSTEMS_OVERLAY
    manifest = json.loads((application / installer.ROOT_MANIFEST).read_text())
    assert manifest["registration"]["overlay"]["sha256"] == installer.checksum(overlay)


def test_update_recovers_crash_after_registration_migration(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    overlay = tmp_path / installer.OVERLAY_REL
    overlay.write_text("<systemList><system /></systemList>\n")
    manifest = json.loads((application / installer.ROOT_MANIFEST).read_text())
    manifest["registration"]["overlay"]["sha256"] = installer.checksum(overlay)
    (application / installer.ROOT_MANIFEST).write_bytes(installer._json_bytes(manifest))
    real_atomic_write = installer._atomic_write_at

    def crash_after_overlay(directory_fd, name, data, mode):
        real_atomic_write(directory_fd, name, data, mode)
        if name == installer.OVERLAY_REL.name:
            raise KeyboardInterrupt("crash")

    monkeypatch.setattr(installer, "_atomic_write_at", crash_after_overlay)
    with pytest.raises(KeyboardInterrupt, match="crash"):
        install(tmp_path, source_root, "two", "stable")

    monkeypatch.setattr(installer, "_atomic_write_at", real_atomic_write)
    pygame = tmp_path / "roms/pygame"
    pygame.chmod(0o777)
    with pytest.raises(RuntimeError, match="unsafe pygame directory"):
        install(tmp_path, source_root, "two", "stable")
    assert (application / installer.LIFECYCLE_JOURNAL).is_file()
    pygame.chmod(0o775)
    install(tmp_path, source_root, "two", "stable")
    assert overlay.read_text() == installer.SYSTEMS_OVERLAY
    assert not (application / installer.LIFECYCLE_JOURNAL).exists()


def test_update_migrates_v3_from_legacy_rom_root(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = make_legacy_v3(tmp_path)
    (tmp_path / installer.LEGACY_APP_PARENT_REL).chmod(0o775)

    install(tmp_path, source_root, "two", "stable")

    assert not launcher.exists()
    manifest = json.loads((app(tmp_path) / installer.ROOT_MANIFEST).read_text())
    assert manifest["version"] == 7
    assert installer.LEGACY_LAUNCHER in manifest["static"]
    assert current(tmp_path)["release_id"] == "two"
    assert manifest["releases"] == ["one", "two"]


def test_legacy_migration_removes_only_owned_pygame_entry(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    make_legacy_v3(tmp_path)
    gamelist = tmp_path / installer.LEGACY_APP_PARENT_REL / "gamelist.xml"
    gamelist.write_text(
        "<gameList>"
        f"<game><path>{installer.LEGACY_APP_GAME_PATH}</path><name>Control Freak</name></game>"
        "<game><path>./other.pygame</path><name>Other</name></game>"
        "</gameList>"
    )

    install(tmp_path, source_root, "two", "stable")

    games = ET.parse(gamelist).getroot().findall("game")
    assert [(game.findtext("path"), game.findtext("name")) for game in games] == [
        ("./other.pygame", "Other"),
        (installer.PYGAME_GAME_PATH, "Control Freak"),
    ]


def test_legacy_migration_recovers_crash_after_rename(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    make_legacy_v3(tmp_path)
    gamelist = tmp_path / installer.LEGACY_APP_PARENT_REL / "gamelist.xml"
    gamelist.write_text(
        "<gameList>"
        f"<game><path>{installer.LEGACY_APP_GAME_PATH}</path><name>Control Freak</name></game>"
        "</gameList>"
    )
    real_rename = installer.os.rename

    def crash_after_rename(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise SystemExit("crash")

    monkeypatch.setattr(installer.os, "rename", crash_after_rename)
    with pytest.raises(SystemExit, match="crash"):
        install(tmp_path, source_root, "two", "stable")
    monkeypatch.setattr(installer.os, "rename", real_rename)
    install(tmp_path, source_root, "two", "stable")
    games = ET.parse(gamelist).getroot().findall("game")
    assert [game.findtext("path") for game in games] == [installer.PYGAME_GAME_PATH]


def test_install_refuses_registration_collision_without_rom_content(tmp_path: Path, source_root: Path) -> None:
    overlay = tmp_path / installer.OVERLAY_REL
    overlay.parent.mkdir(parents=True)
    (tmp_path / "system").chmod(0o755)
    (tmp_path / "system/configs").chmod(0o755)
    overlay.parent.chmod(0o700)
    overlay.write_text("unknown")

    with pytest.raises(RuntimeError, match="unknown registration file: overlay"):
        install(tmp_path, source_root, "one", "stable")

    assert overlay.read_text() == "unknown"
    assert not app(tmp_path).exists()
    assert not (tmp_path / "roms").exists()


def test_uninstall_preserves_unknown_library_files(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    unknown = tmp_path / installer.LIBRARY_REL / "keep.txt"
    unknown.write_text("keep")

    uninstall(tmp_path)

    assert unknown.read_text() == "keep"
    assert not (tmp_path / installer.OVERLAY_REL).exists()
    assert not app(tmp_path).exists()


def test_update_accepts_emulationstation_gamelist_play_metadata(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    gamelist = installer._registration_paths(tmp_path)["gamelist"]
    tree = ET.parse(gamelist)
    game = tree.getroot().find("game")
    assert game is not None
    for tag, text in (
        ("playcount", "2"),
        ("lastplayed", "20261126T170445"),
        ("gametime", "2430"),
        ("lang", "en"),
    ):
        ET.SubElement(game, tag).text = text
    tree.write(gamelist, encoding="utf-8", xml_declaration=True)

    install(tmp_path, source_root, "two", "stable")

    game = ET.parse(gamelist).getroot().find("game")
    assert game is not None
    assert game.findtext("playcount") == "2"
    assert game.findtext("lastplayed") == "20261126T170445"
    assert game.findtext("gametime") == "2430"
    assert game.findtext("lang") == "en"

    native = tmp_path / "roms/pygame/gamelist.xml"
    native_tree = ET.parse(native)
    native_game = native_tree.getroot().find("game")
    assert native_game is not None
    ET.SubElement(native_game, "playcount").text = "3"
    other = ET.Element("game")
    ET.SubElement(other, "path").text = "./other.pygame"
    ET.SubElement(other, "name").text = "Other"
    native_tree.getroot().insert(0, other)
    native_tree.write(native, encoding="utf-8", xml_declaration=True)

    install(tmp_path, source_root, "three", "stable")

    native_games = ET.parse(native).getroot().findall("game")
    assert [item.findtext("name") for item in native_games] == ["Other", "Control Freak"]
    assert native_games[1].findtext("playcount") == "3"


def test_shared_gamelist_refuses_world_writable_file_before_install(tmp_path: Path, source_root: Path) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o775)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./other.pygame</path><name>Other</name></game></gameList>\n")
    gamelist.chmod(0o666)

    with pytest.raises(RuntimeError, match="unsafe gamelist.xml"):
        install(tmp_path, source_root, "one", "stable")

    assert gamelist.stat().st_mode & 0o777 == 0o666
    assert not app(tmp_path).exists()


def test_shared_gamelist_refuses_world_writable_parent_before_snapshot(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o777)
    monkeypatch.setattr(installer, "_snapshot_source", lambda *_args: pytest.fail("source snapshot started"))

    with pytest.raises(RuntimeError, match="unsafe pygame directory"):
        install(tmp_path, source_root, "one", "stable")

    assert not app(tmp_path).exists()


def test_update_and_uninstall_refuse_world_writable_pygame_parent(
    tmp_path: Path, source_root: Path,
) -> None:
    install(tmp_path, source_root, "one", "stable")
    pygame = tmp_path / "roms/pygame"
    pygame.chmod(0o777)

    with pytest.raises(RuntimeError, match="unsafe pygame directory"):
        install(tmp_path, source_root, "two", "stable")
    with pytest.raises(RuntimeError, match="unsafe pygame directory"):
        uninstall(tmp_path)

    assert current(tmp_path)["release_id"] == "one"
    assert app(tmp_path).is_dir()


def test_shared_gamelist_allows_batocera_roms_parent_mode(tmp_path: Path, source_root: Path) -> None:
    roms = tmp_path / "roms"
    pygame = roms / "pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    roms.chmod(0o2770)
    pygame.chmod(0o2755)

    install(tmp_path, source_root, "one", "stable")

    assert (pygame / "gamelist.xml").is_file()


def test_platform_owned_group_writable_gamelist_survives_full_lifecycle(
    tmp_path: Path, source_root: Path,
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o775)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./other.pygame</path><name>Other</name></game></gameList>\n")
    gamelist.chmod(0o664)

    install(tmp_path, source_root, "one", "stable")
    install(tmp_path, source_root, "two", "stable")
    uninstall(tmp_path)

    assert pygame.stat().st_mode & 0o777 == 0o775
    assert gamelist.stat().st_mode & 0o777 == 0o664
    assert [game.findtext("name") for game in ET.parse(gamelist).getroot().findall("game")] == ["Other"]


def test_gamelist_state_accepts_non_euid_platform_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o664, st_uid=os.geteuid() + 1)
    monkeypatch.setattr(installer, "_read_at", lambda *_args: (b"<gameList />", info))

    assert installer._gamelist_state(1) == (b"<gameList />", 0o664, info)


def test_gamelist_state_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    pygame = tmp_path / "pygame"
    pygame.mkdir()
    os.mkfifo(pygame / "gamelist.xml")
    probe = (
        "import os,sys;from deploy import install_batocera as i;"
        "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);"
        "i._gamelist_state(fd)"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe, str(pygame)], capture_output=True, text=True, timeout=2,
    )

    assert result.returncode != 0
    assert "Expected regular file: gamelist.xml" in result.stderr


@pytest.mark.parametrize("controlled", [
    f"<game><path>{installer.PYGAME_GAME_PATH}</path><name><b>Control Freak</b></name></game>",
    f"<game><path>{installer.PYGAME_GAME_PATH}</path><path>{installer.PYGAME_GAME_PATH}</path><name>Control Freak</name></game>",
    f"<game><path>{installer.PYGAME_GAME_PATH}</path><name>Control Freak</name><unknown>bad</unknown></game>",
])
def test_native_gamelist_semantics_fail_before_source_snapshot(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch, controlled: str,
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o775)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_text(f"<gameList>{controlled}</gameList>\n")
    gamelist.chmod(0o664)
    monkeypatch.setattr(installer, "_snapshot_source", lambda *_args: pytest.fail("source snapshot started"))

    with pytest.raises(RuntimeError, match="Malformed Control Freak gamelist entry"):
        install(tmp_path, source_root, "one", "stable")

    assert gamelist.read_text() == f"<gameList>{controlled}</gameList>\n"
    assert not app(tmp_path).exists()
    assert not (tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME).exists()
    assert not any(path.exists() for path in installer._registration_paths(tmp_path).values())


def test_shared_gamelist_concurrent_write_is_not_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    real = installer._exchange_paths_at
    injected = False

    def exchange_with_concurrent_change(directory_fd: int, left: str, right: str) -> None:
        nonlocal injected
        if right == "gamelist.xml" and not injected:
            injected = True
            gamelist.write_bytes(concurrent)
        real(directory_fd, left, right)

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_concurrent_change)
    with pytest.raises(RuntimeError, match="gamelist.xml changed during operation"):
        installer._sync_pygame_gamelist(tmp_path)
    assert gamelist.read_bytes() == concurrent


def test_shared_gamelist_atomic_replacement_is_not_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    real = installer._exchange_paths_at
    injected = False

    def exchange_with_concurrent_replacement(directory_fd: int, left: str, right: str) -> None:
        nonlocal injected
        if right == "gamelist.xml" and not injected:
            injected = True
            replacement = pygame / "concurrent.xml"
            replacement.write_bytes(concurrent)
            os.replace(replacement, gamelist)
        real(directory_fd, left, right)

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_concurrent_replacement)
    with pytest.raises(RuntimeError, match="gamelist.xml changed during operation"):
        installer._sync_pygame_gamelist(tmp_path)
    assert gamelist.read_bytes() == concurrent


def test_shared_gamelist_atomic_replacement_survives_post_exchange_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    original = b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n"
    gamelist.write_bytes(original)
    gamelist.chmod(0o644)
    candidate = installer.gamelist_bytes(original, "registration")
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    real_exchange = installer._exchange_paths_at
    real_fsync = os.fsync
    fail_directory_fsync = False
    injected = False

    def fsync_with_failure(fd: int) -> None:
        nonlocal injected
        if fail_directory_fsync:
            if not injected:
                injected = True
                replacement = pygame / "concurrent.xml"
                replacement.write_bytes(concurrent)
                os.replace(replacement, gamelist)
            raise OSError("injected post-exchange fsync failure")
        real_fsync(fd)

    def exchange_with_concurrent_replacement(directory_fd: int, left: str, right: str) -> None:
        nonlocal fail_directory_fsync
        if right == "gamelist.xml":
            fail_directory_fsync = True
        real_exchange(directory_fd, left, right)

    monkeypatch.setattr(os, "fsync", fsync_with_failure)
    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_concurrent_replacement)
    with pytest.raises(OSError, match="injected post-exchange fsync failure"):
        installer._sync_pygame_gamelist(tmp_path)
    survivors = [gamelist, *pygame.glob(".gamelist.xml.tmp-*")]
    assert any(path.read_bytes() == concurrent for path in survivors)
    assert any(path.read_bytes() == original for path in survivors)
    assert any(path.read_bytes() == candidate for path in survivors)


def test_shared_gamelist_open_writer_is_named_after_final_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    writer = os.open(gamelist, os.O_WRONLY)
    real_read_fd = installer._read_fd
    reads = 0

    def read_with_late_write(fd: int) -> tuple[bytes, os.stat_result]:
        nonlocal reads
        result = real_read_fd(fd)
        reads += 1
        if reads == 3:
            os.ftruncate(writer, 0)
            os.write(writer, concurrent)
            os.fsync(writer)
        return result

    monkeypatch.setattr(installer, "_read_fd", read_with_late_write)
    try:
        installer._sync_pygame_gamelist(tmp_path)
    finally:
        os.close(writer)
    assert any(path.read_bytes() == concurrent for path in pygame.glob(".gamelist.xml.tmp-*"))


def test_shared_gamelist_displaced_replacement_cannot_orphan_late_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    writer = os.open(gamelist, os.O_WRONLY)
    real_read_fd = installer._read_fd
    reads = 0

    def read_with_displaced_replacement(fd: int) -> tuple[bytes, os.stat_result]:
        nonlocal reads
        result = real_read_fd(fd)
        reads += 1
        if reads == 3:
            displaced = next(pygame.glob(".gamelist.xml.tmp-*"))
            competitor = pygame / "competitor.xml"
            competitor.write_bytes(b"COMPETITOR")
            os.replace(competitor, displaced)
            os.ftruncate(writer, 0)
            os.write(writer, concurrent)
            os.fsync(writer)
        return result

    monkeypatch.setattr(installer, "_read_fd", read_with_displaced_replacement)
    try:
        installer._sync_pygame_gamelist(tmp_path)
    finally:
        os.close(writer)
    survivors = [gamelist, *pygame.glob(".gamelist.xml.tmp-*")]
    assert any(path.read_bytes() == concurrent for path in survivors)


def test_activation_restore_preserves_late_writer_to_existing_gamelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    expected = b"<gameList><game><path>./control-freak.pygame</path></game></gameList>\n"
    previous = b"<gameList><game><path>./other.pygame</path></game></gameList>\n"
    concurrent = b"<gameList><game><path>./late.pygame</path></game></gameList>\n"
    gamelist.write_bytes(expected)
    gamelist.chmod(0o644)
    writer = os.open(gamelist, os.O_WRONLY)
    real_exchange = installer._exchange_paths_at
    injected = False

    def exchange_with_late_writer(directory_fd: int, left: str, right: str) -> None:
        nonlocal injected
        real_exchange(directory_fd, left, right)
        if right == "gamelist.xml" and not injected:
            injected = True
            os.ftruncate(writer, 0)
            os.write(writer, concurrent)
            os.fsync(writer)

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_late_writer)
    try:
        with pytest.raises(RuntimeError, match="gamelist.xml changed during operation"):
            installer._restore_pygame_gamelist(tmp_path, (expected, 0o644), (previous, 0o644))
    finally:
        os.close(writer)
    survivors = [gamelist, *pygame.glob(".gamelist.xml.tmp-*")]
    assert any(path.read_bytes() == concurrent for path in survivors)


def test_activation_restore_preserves_late_writer_when_previous_gamelist_was_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    expected = installer.gamelist_bytes(None, "registration")
    concurrent = b"<gameList><game><path>./late.pygame</path></game></gameList>\n"
    gamelist.write_bytes(expected)
    gamelist.chmod(0o644)
    writer = os.open(gamelist, os.O_WRONLY)
    real_exchange = installer._exchange_paths_at
    injected = False

    def exchange_with_late_writer(directory_fd: int, left: str, right: str) -> None:
        nonlocal injected
        real_exchange(directory_fd, left, right)
        if right == "gamelist.xml" and not injected:
            injected = True
            os.ftruncate(writer, 0)
            os.write(writer, concurrent)
            os.fsync(writer)

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_late_writer)
    try:
        with pytest.raises(RuntimeError, match="gamelist.xml changed during operation"):
            installer._restore_pygame_gamelist(tmp_path, (expected, 0o644), (None, 0o644))
    finally:
        os.close(writer)
    survivors = [path for path in [gamelist, *pygame.glob(".gamelist.xml.tmp-*")] if path.exists()]
    assert any(path.read_bytes() == concurrent for path in survivors)
    assert gamelist.exists()
    assert installer.PYGAME_GAME_PATH not in gamelist.read_text()


def test_shared_gamelist_open_writer_survives_exchange_fsync_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    writer = os.open(gamelist, os.O_WRONLY)
    real_exchange = installer._exchange_paths_at
    real_fsync = os.fsync
    injected = False

    def exchange_with_late_write(directory_fd: int, left: str, right: str) -> None:
        nonlocal injected
        real_exchange(directory_fd, left, right)
        if right == "gamelist.xml" and not injected:
            injected = True
            os.ftruncate(writer, 0)
            os.write(writer, concurrent)
            real_fsync(writer)

    def fsync_with_exchange_failure(fd: int) -> None:
        if injected and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected exchange directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_late_write)
    monkeypatch.setattr(os, "fsync", fsync_with_exchange_failure)
    try:
        with pytest.raises(OSError, match="injected exchange directory fsync failure"):
            installer._sync_pygame_gamelist(tmp_path)
    finally:
        os.close(writer)
    assert gamelist.read_bytes() == concurrent


def test_shared_gamelist_failed_exchange_rollback_preserves_displaced_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./one.pygame</path><name>One</name></game></gameList>\n")
    gamelist.chmod(0o644)
    concurrent = b"<gameList><game><path>./two.pygame</path><name>Two</name></game></gameList>\n"
    real = installer._exchange_paths_at
    exchanges = 0

    def exchange_with_failed_rollback(directory_fd: int, left: str, right: str) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            gamelist.write_bytes(concurrent)
            real(directory_fd, left, right)
            return
        raise OSError("injected rollback failure")

    monkeypatch.setattr(installer, "_exchange_paths_at", exchange_with_failed_rollback)
    with pytest.raises(OSError, match="injected rollback failure"):
        installer._sync_pygame_gamelist(tmp_path)
    assert any(path.read_bytes() == concurrent for path in pygame.glob(".gamelist.xml.tmp-*"))


@pytest.mark.parametrize(
    "controlled",
    [
        f'<game bad="1"><path>{installer.PYGAME_GAME_PATH}</path><name>Control Freak</name></game>',
        f'<game><path>{installer.PYGAME_GAME_PATH}</path><name>Control Freak</name><unknown>x</unknown></game>',
        f'<game><path>{installer.PYGAME_GAME_PATH}</path><path>./other.pygame</path><name>Control Freak</name></game>',
        f'<game><path>{installer.PYGAME_GAME_PATH}</path><name><nested>Control Freak</nested></name></game>',
    ],
)
def test_shared_gamelist_refuses_malformed_controlled_entry(controlled: str) -> None:
    data = f"<gameList>{controlled}</gameList>\n".encode()

    with pytest.raises(RuntimeError, match="Malformed Control Freak gamelist entry"):
        installer.gamelist_bytes(data, "registration")


def test_update_refuses_gamelist_fixed_metadata_drift(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    gamelist = installer._registration_paths(tmp_path)["gamelist"]
    tree = ET.parse(gamelist)
    game = tree.getroot().find("game")
    assert game is not None
    path = game.find("path")
    assert path is not None
    path.text = "./Other.controlfreak"
    tree.write(gamelist, encoding="utf-8", xml_declaration=True)

    with pytest.raises(RuntimeError, match="Modified registration file: gamelist"):
        install(tmp_path, source_root, "two", "stable")


@pytest.mark.parametrize("location", ["root", "game", "tail"])
def test_gamelist_ownership_rejects_mixed_content(tmp_path: Path, location: str) -> None:
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_bytes(installer._library_gamelist())
    tree = ET.parse(gamelist)
    root = tree.getroot()
    game = root.find("game")
    assert game is not None
    if location == "root":
        root.text = "unexpected"
    elif location == "game":
        game.text = "unexpected"
    else:
        game[0].tail = "unexpected"
    tree.write(gamelist, encoding="utf-8", xml_declaration=True)

    assert installer._gamelist_is_owned(gamelist) is False


@pytest.mark.parametrize("name", ["overlay", "marker", "gamelist", "image"])
def test_update_refuses_registration_drift(tmp_path: Path, source_root: Path, name: str) -> None:
    install(tmp_path, source_root, "one", "stable")
    path = installer._registration_paths(tmp_path)[name]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match=f"Modified registration file: {name}"):
        install(tmp_path, source_root, "two", "stable")


@pytest.mark.parametrize("name", ["overlay", "marker", "gamelist", "image"])
def test_uninstall_refuses_registration_drift(tmp_path: Path, source_root: Path, name: str) -> None:
    install(tmp_path, source_root, "one", "stable")
    path = installer._registration_paths(tmp_path)[name]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match=f"Modified registration file: {name}"):
        uninstall(tmp_path)
    assert app(tmp_path).is_dir()


def test_install_refuses_registration_symlink(tmp_path: Path, source_root: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("safe")
    marker = tmp_path / installer.LIBRARY_REL / installer.MARKER_NAME
    marker.parent.mkdir(parents=True)
    (tmp_path / "system").chmod(0o755)
    (tmp_path / "system/control-freak").chmod(0o700)
    marker.parent.chmod(0o700)
    marker.symlink_to(outside)
    with pytest.raises(OSError):
        install(tmp_path, source_root, "one", "stable")
    assert outside.read_text() == "safe"


def test_install_refuses_executable_marker_collision(tmp_path: Path, source_root: Path) -> None:
    marker = tmp_path / installer.LIBRARY_REL / installer.MARKER_NAME
    marker.parent.mkdir(parents=True)
    (tmp_path / "system").chmod(0o755)
    (tmp_path / "system/control-freak").chmod(0o700)
    marker.parent.chmod(0o700)
    marker.write_bytes(b"")
    marker.chmod(0o755)
    with pytest.raises(RuntimeError, match="unknown registration file: marker"):
        install(tmp_path, source_root, "one", "stable")


def test_registration_manifest_is_exact_schema(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest_path = app(tmp_path) / installer.ROOT_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["registration"]["overlay"]["extra"] = True
    manifest_path.write_bytes(installer._json_bytes(manifest))
    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)


def test_uninstall_removes_every_owned_registration_file(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    paths = list(installer._registration_paths(tmp_path).values())
    native = tmp_path / "roms/pygame/gamelist.xml"
    tree = ET.parse(native)
    other = ET.SubElement(tree.getroot(), "game")
    ET.SubElement(other, "path").text = "./other.pygame"
    ET.SubElement(other, "name").text = "Other"
    tree.write(native, encoding="utf-8", xml_declaration=True)
    uninstall(tmp_path)
    assert all(not path.exists() for path in paths)
    assert [game.findtext("name") for game in ET.parse(native).getroot().findall("game")] == ["Other"]


def test_library_contains_no_executable_files(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    assert all(not path.is_file() or path.stat().st_mode & 0o111 == 0 for path in (tmp_path / installer.LIBRARY_REL).rglob("*"))


def test_install_refuses_duplicate_legacy_and_current_roots(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    legacy = tmp_path / installer.LEGACY_APP_REL
    legacy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app(tmp_path), legacy)
    with pytest.raises(RuntimeError, match="duplicate Control Freak installations"):
        install(tmp_path, source_root, "two", "stable")


def test_unauthenticated_fresh_source_leaves_no_registration(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path / "source", source_root)
    target = source / "src/control_freak/app.py"
    target.write_text(target.read_text() + "\n# unsigned\n")
    with pytest.raises(RuntimeError, match="trust statement"):
        install(tmp_path / "root", source, "one", "stable")
    assert not (tmp_path / "root" / installer.OVERLAY_REL).exists()
    assert not (tmp_path / "root" / installer.LIBRARY_REL).exists()


def test_unauthenticated_source_does_not_migrate_legacy_install(tmp_path: Path, source_root: Path) -> None:
    root = tmp_path / "root"
    install(root, source_root, "one", "stable")
    make_legacy_v3(root)
    source = copied_source(tmp_path / "source", source_root)
    (source / "src/control_freak/app.py").write_bytes(b"unsigned")

    with pytest.raises(RuntimeError, match="trust statement"):
        install(root, source, "two", "stable")

    assert (root / installer.LEGACY_APP_REL).is_dir()
    assert not app(root).exists()


def test_fresh_registration_preflights_every_collision_before_publish(tmp_path: Path, source_root: Path) -> None:
    image = tmp_path / installer.LIBRARY_REL / "images/control-freak.png"
    image.parent.mkdir(parents=True)
    (tmp_path / "system").chmod(0o755)
    (tmp_path / "system/control-freak").chmod(0o700)
    (tmp_path / installer.LIBRARY_REL).chmod(0o700)
    image.parent.chmod(0o700)
    image.write_bytes(b"unknown")

    with pytest.raises(RuntimeError, match="unknown registration file: image"):
        install(tmp_path, source_root, "one", "stable")

    assert not (tmp_path / installer.OVERLAY_REL).exists()
    assert not (tmp_path / installer.LIBRARY_REL / installer.MARKER_NAME).exists()
    assert image.read_bytes() == b"unknown"


def test_fresh_registration_compensates_caught_publish_failure(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = installer._atomic_create_at

    def fail(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == "gamelist.xml":
            raise OSError("publish failed")
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_create_at", fail)
    with pytest.raises(OSError, match="publish failed"):
        install(tmp_path, source_root, "one", "stable")

    assert all(not path.exists() for path in installer._registration_paths(tmp_path).values())


def test_update_preserves_modified_v3_launcher(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = make_legacy_v3(tmp_path)
    launcher.write_text("unknown")

    with pytest.raises(RuntimeError, match="Modified stable file: control-freak.pygame"):
        install(tmp_path, source_root, "two", "stable")

    assert launcher.read_text() == "unknown"
    assert json.loads((tmp_path / installer.LEGACY_APP_REL / installer.ROOT_MANIFEST).read_text())["version"] == 3


def test_v3_migration_recovers_after_launcher_replace(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = make_legacy_v3(tmp_path)
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.ROOT_MANIFEST and (app(tmp_path) / installer.LIFECYCLE_JOURNAL).is_file():
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(tmp_path, source_root, "two", "stable")
    assert not launcher.exists()
    assert (app(tmp_path) / installer.LIFECYCLE_JOURNAL).is_file()

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    install(tmp_path, source_root, "two", "stable")
    assert json.loads((app(tmp_path) / installer.ROOT_MANIFEST).read_text())["version"] == 7
    assert not (app(tmp_path) / installer.LIFECYCLE_JOURNAL).exists()


def test_v3_migration_replaces_service_and_launcher(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = make_legacy_v3(tmp_path)
    service = tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME
    service.write_bytes(b"#!/bin/sh\nexit 0\n")
    service.chmod(installer.SERVICE_MODE)
    manifest_path = tmp_path / installer.LEGACY_APP_REL / installer.ROOT_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["service"]["sha256"] = installer.checksum(service)
    manifest_path.write_bytes(installer._json_bytes(manifest))
    install(tmp_path, source_root, "two", "stable")
    assert not launcher.exists()
    assert service.read_bytes() == installer._service_bytes(source_root, tmp_path)
    assert json.loads((app(tmp_path) / installer.ROOT_MANIFEST).read_text())["version"] == 7


def test_v5_rejects_modified_app_root_launcher(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = app(tmp_path) / installer.LEGACY_LAUNCHER
    launcher.write_text("unknown")

    with pytest.raises(RuntimeError, match="Modified stable file"):
        install(tmp_path, source_root, "two", "stable")
    with pytest.raises(RuntimeError, match="Modified stable file"):
        uninstall(tmp_path)

    assert launcher.read_text() == "unknown"


def test_install_normalizes_safe_state_directory_and_rejects_writable_one(tmp_path: Path, source_root: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_state = safe_root / "system/control-freak"
    safe_state.mkdir(parents=True, mode=0o755)
    safe_state.parent.chmod(0o755)
    install(safe_root, source_root, "one", "stable")
    assert safe_state.stat().st_mode & 0o777 == 0o700

    unsafe_root = tmp_path / "unsafe"
    unsafe_state = unsafe_root / "system/control-freak"
    unsafe_state.mkdir(parents=True, mode=0o777)
    unsafe_state.parent.chmod(0o755)
    unsafe_state.chmod(0o777)
    with pytest.raises(RuntimeError, match="unsafe Control Freak state directory"):
        install(unsafe_root, source_root, "one", "stable")
    assert not app(unsafe_root).exists()


def test_install_normalizes_runtime_modes_before_trust_verification(tmp_path: Path, source_root: Path) -> None:
    source_root = copied_source(tmp_path, source_root)
    source = source_root / "src/control_freak/assets/fonts/DejaVuSans.ttf"
    source.chmod(0o664)

    install(tmp_path, source_root, "one", "stable")

    installed = app(tmp_path) / "releases/one/control_freak/assets/fonts/DejaVuSans.ttf"
    assert installed.stat().st_mode & 0o777 == 0o644


def test_install_manages_logo_proposals(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    root = app(tmp_path)
    manifest = json.loads((root / "install-manifest.json").read_text())

    assert manifest["static"].keys() >= {
        "logo-proposal-1.png",
        "logo-proposal-2.png",
        "logo-proposal-3.png",
        "logo-proposal-4.png",
        "logo-proposal-5.png",
        "logo-proposal-6.png",
    }
    assert (root / "image.png").read_bytes() == (root / "logo-proposal-1.png").read_bytes()


def test_install_and_uninstall_manage_web_service(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    service = tmp_path / "system/services/control_freak"

    assert service.is_file()
    assert service.stat().st_mode & 0o777 == 0o755

    uninstall(tmp_path)
    assert not service.exists()


def test_fresh_install_service_start_failure_rolls_back_publication(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    original_gamelist = b"<gameList><game><path>./other.pygame</path><name>Other</name></game></gameList>\n"
    native_gamelist = pygame / "gamelist.xml"
    native_gamelist.write_bytes(original_gamelist)
    native_gamelist.chmod(0o644)
    calls = []
    service = {"enabled": False, "running": False}
    probes = []

    def actions(_root: Path, *requested: str) -> None:
        calls.append(requested)
        for action in requested:
            if action == "enable":
                service["enabled"] = True
            elif action == "disable":
                service["enabled"] = False
            elif action == "stop":
                service["running"] = False
            elif action == "start":
                raise RuntimeError("start failed")

    monkeypatch.setattr(installer, "_service_actions", actions)
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: probes.append("enabled") or service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: probes.append("running") or service["running"])

    with pytest.raises(RuntimeError, match="start failed"):
        install(tmp_path, source_root, "one", "stable")

    assert not app(tmp_path).exists()
    assert not (tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME).exists()
    assert not (tmp_path / "system/control-freak/library/gamelist.xml").exists()
    native = tmp_path / "roms/pygame/gamelist.xml"
    assert native.read_bytes() == original_gamelist
    assert calls == [("enable", "start"), ("stop",), ("disable",)]
    assert service == {"enabled": False, "running": False}
    assert probes == ["enabled", "running"]


def test_fresh_install_retains_complete_install_when_service_compensation_fails(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installer,
        "_service_actions",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("service action failed")),
    )

    with pytest.raises(RuntimeError, match="activation rollback failed; complete installation retained"):
        install(tmp_path, source_root, "one", "stable")

    assert app(tmp_path).is_dir()
    assert (tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME).is_file()
    assert registered(tmp_path)


def test_update_activation_failure_restores_previous_pointer(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    calls = []

    def actions(_root: Path, *requested: str) -> None:
        calls.append(requested)
        if requested == ("start",) and calls.count(("start",)) == 1:
            raise RuntimeError("start failed")

    monkeypatch.setattr(installer, "_service_actions", actions)
    with pytest.raises(RuntimeError, match="start failed"):
        install(tmp_path, source_root, "two", "nightly")

    assert current(tmp_path) == {"version": 1, "release_id": "one", "channel": "stable"}
    assert calls == [("stop",), ("start",), ("stop",), ("start",)]


def test_update_preserves_disabled_service_state(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    actions = []
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: False)
    monkeypatch.setattr(installer, "_service_running", lambda _root: False)
    monkeypatch.setattr(installer, "_service_actions", lambda _root, *requested: actions.append(requested))

    install(tmp_path, source_root, "two", "nightly")

    assert current(tmp_path) == {"version": 1, "release_id": "two", "channel": "nightly"}
    assert actions == []


def test_update_preserves_disabled_but_running_service_state(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    actions = []
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: False)
    monkeypatch.setattr(installer, "_service_running", lambda _root: True)
    monkeypatch.setattr(installer, "_service_actions", lambda _root, *requested: actions.append(requested))

    install(tmp_path, source_root, "two", "nightly")

    assert actions == [("stop",), ("start",)]


def test_fresh_install_rejects_valid_uninstall_quarantine(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pygame = tmp_path / "system/control-freak"
    app(tmp_path).rename(pygame / ".control-freak-remove-0123456789abcdef01234567")

    with pytest.raises(RuntimeError, match="quarantine requires recovery"):
        install(tmp_path, source_root, "two", "stable")


def test_interrupted_update_activation_recovers_selected_pointer(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    install(tmp_path, source_root, "two", "nightly")
    app_fd = installer._open_dir(app(tmp_path))
    try:
        installer._atomic_write_at(
            app_fd,
            installer.ACTIVATION_JOURNAL,
            installer._json_bytes({
                "version": 1,
                "before": {"version": 1, "release_id": "one", "channel": "stable"},
                "after": {"version": 1, "release_id": "two", "channel": "nightly"},
            }),
            0o600,
        )
        installer._atomic_write_at(
            app_fd,
            installer.CURRENT,
            installer._json_bytes({"version": 1, "release_id": "two", "channel": "nightly"}),
            0o600,
        )
    finally:
        installer.os.close(app_fd)
    for name, mode in installer.STATIC_MODES.items():
        (app(tmp_path) / name).chmod(mode | 0o020)
    calls = []

    def actions(_root: Path, *requested: str) -> None:
        assert all((app(tmp_path) / name).stat().st_mode & 0o777 == mode for name, mode in installer.STATIC_MODES.items())
        calls.append(requested)

    monkeypatch.setattr(installer, "_service_actions", actions)

    installer._recover_activation(tmp_path, app(tmp_path))

    assert current(tmp_path) == {"version": 1, "release_id": "two", "channel": "nightly"}
    assert not (app(tmp_path) / installer.ACTIVATION_JOURNAL).exists()
    assert calls == [("stop", "start")]


@pytest.mark.parametrize(
    "enabled,running,expected",
    [
        (False, False, [("disable",), ("stop",)]),
        (False, True, [("disable",), ("start",)]),
        (True, False, [("enable",), ("stop",)]),
        (True, True, [("enable",), ("start",)]),
    ],
)
def test_interrupted_activation_restores_exact_journaled_service_state(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    running: bool,
    expected: list[tuple[str, ...]],
) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = current(tmp_path)
    (app(tmp_path) / installer.ACTIVATION_JOURNAL).write_bytes(installer._json_bytes({
        "version": 2,
        "before": pointer,
        "after": pointer,
        "service_enabled": enabled,
        "service_running": running,
    }))
    calls = []
    service = {"enabled": not enabled, "running": not running}
    probes = []

    def actions(_root: Path, *requested: str) -> None:
        calls.append(requested)
        service["enabled"] = requested[0] == "enable" if requested[0] in {"enable", "disable"} else service["enabled"]
        service["running"] = requested[0] == "start" if requested[0] in {"start", "stop"} else service["running"]

    monkeypatch.setattr(installer, "_service_actions", actions)
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: probes.append("enabled") or service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: probes.append("running") or service["running"])

    installer._recover_activation(tmp_path, app(tmp_path))

    assert calls == expected
    assert probes == ["enabled", "running"]
    assert not (app(tmp_path) / installer.ACTIVATION_JOURNAL).exists()


def test_interrupted_activation_refuses_modified_service_before_actions(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    app_fd = installer._open_dir(application)
    try:
        pointer = current(tmp_path)
        installer._atomic_write_at(
            app_fd,
            installer.ACTIVATION_JOURNAL,
            installer._json_bytes({"version": 1, "before": pointer, "after": pointer}),
            0o600,
        )
    finally:
        installer.os.close(app_fd)
    service = tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME
    service.write_bytes(service.read_bytes() + b"\n# modified\n")
    calls = []
    monkeypatch.setattr(installer, "_service_actions", lambda _root, *actions: calls.append(actions))

    with pytest.raises(RuntimeError, match="Modified service file"):
        installer._recover_activation(tmp_path, application)

    assert calls == []
    assert (application / installer.ACTIVATION_JOURNAL).exists()


def test_uninstall_accepts_v2_install_without_services_directory(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    make_legacy_v3(tmp_path)
    manifest_path = tmp_path / installer.LEGACY_APP_REL / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps({"version": 2, "static": manifest["static"], "releases": manifest["releases"]}))
    service = tmp_path / "system/services/control_freak"
    service.unlink()
    service.parent.rmdir()

    install(tmp_path, source_root, "two", "stable")
    uninstall(tmp_path)

    assert not app(tmp_path).exists()


def test_install_migrates_version_two_manifest_to_owned_service(tmp_path: Path, source_root: Path) -> None:
    old_source = copied_source(tmp_path / "old", source_root)
    new_source = copied_source(tmp_path / "new", source_root)
    private = resign_source(old_source)
    resign_source(new_source, private)
    install(tmp_path / "root", old_source, "one", "stable")
    make_legacy_v3(tmp_path / "root")
    manifest_path = tmp_path / "root" / installer.LEGACY_APP_REL / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps({"version": 2, "static": manifest["static"], "releases": manifest["releases"]}))
    (tmp_path / "root/system/services/control_freak").unlink()
    (new_source / "assets/control-freak-logo-4.png").write_bytes((new_source / "assets/control-freak-logo-5.png").read_bytes())
    installer_script = new_source / "deploy/install_batocera.py"
    installer_script.write_bytes(installer_script.read_bytes() + b"\n# new lifecycle revision\n")
    resign_source(new_source, private)

    install(tmp_path / "root", new_source, "two", "stable")

    migrated = json.loads((app(tmp_path / "root") / "install-manifest.json").read_text())
    assert migrated["version"] == 7
    assert (app(tmp_path / "root") / installer.LEGACY_LAUNCHER).is_file()
    assert migrated["service"]["name"] == "control_freak"
    assert (tmp_path / "root/system/services/control_freak").is_file()
    assert (app(tmp_path / "root") / "logo-proposal-4.png").read_bytes() == (new_source / "assets/control-freak-logo-4.png").read_bytes()


def test_install_updates_authenticated_service_wrapper(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path, source_root)
    install(tmp_path / "root", source, "one", "stable")
    service = tmp_path / "root/system/services/control_freak"
    original = service.read_bytes()
    service_source = source / "deploy/control_freak"
    service_source.write_text(service_source.read_text() + "\n# updated\n")
    resign_source(source)

    install(tmp_path / "root", source, "two", "stable")

    assert service.read_bytes() != original
    assert service.read_bytes() == installer._service_bytes(source, tmp_path / "root")
    assert current(tmp_path / "root")["release_id"] == "two"
    assert json.loads((app(tmp_path / "root") / installer.ROOT_MANIFEST).read_text())["releases"] == ["one", "two"]
    _release, bundle_fd = runpy.run_path(str(service))["validated_release"]()
    os.close(bundle_fd)


def test_service_wrapper_update_recovers_after_atomic_publish(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copied_source(tmp_path, source_root)
    root = tmp_path / "root"
    install(root, source, "one", "stable")
    service_source = source / "deploy/control_freak"
    service_source.write_text(service_source.read_text() + "\n# updated\n")
    resign_source(source)
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        real(directory_fd, name, data, mode)
        if name == installer.SERVICE_NAME:
            raise SystemExit("crash")

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(SystemExit, match="crash"):
        install(root, source, "two", "stable")

    assert (app(root) / installer.LIFECYCLE_JOURNAL).is_file()
    assert (app(root) / "releases/two").is_dir()
    assert "two" in json.loads((app(root) / installer.ROOT_MANIFEST).read_text())["releases"]
    monkeypatch.setattr(installer, "_atomic_write_at", real)
    install(root, source, "two", "stable")

    assert (root / installer.SERVICES_REL / installer.SERVICE_NAME).read_bytes() == installer._service_bytes(source, root)
    assert current(root)["release_id"] == "two"
    assert not (app(root) / installer.LIFECYCLE_JOURNAL).exists()


def test_service_repair_does_not_replace_concurrent_unknown_file(tmp_path: Path, source_root: Path, monkeypatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    service = tmp_path / "system/services/control_freak"
    service.unlink()
    real = installer._atomic_create_at

    def race(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"unknown")
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_create_at", race)
    with pytest.raises(RuntimeError, match="Refusing existing file"):
        install(tmp_path, source_root, "one", "stable")

    assert service.read_bytes() == b"unknown"


def test_uninstall_recovers_interrupted_lifecycle_update(tmp_path: Path, source_root: Path, monkeypatch) -> None:
    source = copied_source(tmp_path, source_root)
    private = resign_source(source)
    root = tmp_path / "root"
    install(root, source, "one", "stable")
    (source / "assets/control-freak-logo-4.png").write_bytes((source / "assets/control-freak-logo-5.png").read_bytes())
    resign_source(source, private)
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.ROOT_MANIFEST and (app(root) / installer.LIFECYCLE_JOURNAL).is_file():
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(root, source, "two", "stable")
    assert (app(root) / installer.LIFECYCLE_JOURNAL).is_file()

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    uninstall(root)

    assert not app(root).exists()
    assert not (root / "system/services/control_freak").exists()


def test_lifecycle_recovery_rejects_boolean_journal_version(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest = json.loads((app(tmp_path) / installer.ROOT_MANIFEST).read_text())
    journal = app(tmp_path) / installer.LIFECYCLE_JOURNAL
    journal.write_text(json.dumps({"version": True, "before": manifest, "after": manifest}))

    with pytest.raises(RuntimeError, match="Invalid lifecycle journal"):
        uninstall(tmp_path)

    assert journal.is_file()


def test_install_manifest_rejects_float_service_mode() -> None:
    static = {name: "0" * 64 for name in installer.STATIC_FILES}
    manifest = {"version": 3, "static": static, "service": {"name": installer.SERVICE_NAME, "sha256": "0" * 64, "mode": float(installer.SERVICE_MODE)}, "releases": ["one"]}

    assert not installer._valid_install_manifest(manifest)


def test_fresh_install_recovers_when_service_publish_was_interrupted(tmp_path: Path, source_root: Path, monkeypatch) -> None:
    real = installer._atomic_create_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.SERVICE_NAME:
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_create_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(tmp_path, source_root, "one", "stable")
    assert app(tmp_path).is_dir()
    assert not (tmp_path / "system/services/control_freak").exists()

    monkeypatch.setattr(installer, "_atomic_create_at", real)
    install(tmp_path, source_root, "one", "stable")

    assert (tmp_path / "system/services/control_freak").is_file()




def test_fresh_install_recovery_compensates_partial_activation(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    installer._fresh_activation(tmp_path, True)
    service = {"enabled": False, "running": False}
    calls = []
    probes = []

    def actions(_root: Path, *requested: str) -> None:
        calls.append(requested)
        action = requested[0]
        if action == "enable":
            service["enabled"] = True
        elif action == "start":
            raise RuntimeError("start failed")
        elif action == "stop":
            service["running"] = False
        elif action == "disable":
            service["enabled"] = False

    monkeypatch.setattr(installer, "_service_actions", actions)
    monkeypatch.setattr(installer, "_service_enabled", lambda _root: probes.append("enabled") or service["enabled"])
    monkeypatch.setattr(installer, "_service_running", lambda _root: probes.append("running") or service["running"])

    with pytest.raises(RuntimeError, match="Unable to activate"):
        install(tmp_path, source_root, "one", "stable")

    assert calls == [("enable",), ("start",), ("disable",), ("stop",)]
    assert service == {"enabled": False, "running": False}
    assert probes == ["enabled", "running"]
    assert (tmp_path / "system/control-freak" / installer.INSTALL_ACTIVATION).is_file()


def test_uninstall_removes_fresh_install_interrupted_before_gamelist(tmp_path: Path, source_root: Path, monkeypatch) -> None:
    real = installer._atomic_create_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.SERVICE_NAME:
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_create_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(tmp_path, source_root, "one", "stable")
    monkeypatch.setattr(installer, "_atomic_create_at", real)

    uninstall(tmp_path)

    assert not app(tmp_path).exists()
    assert not (tmp_path / "system/services/control_freak").exists()


def test_install_refuses_unknown_web_service(tmp_path: Path, source_root: Path) -> None:
    service = tmp_path / "system/services/control_freak"
    service.parent.mkdir(parents=True)
    (tmp_path / "system").chmod(0o755)
    service.parent.chmod(0o755)
    service.write_text("local service")

    with pytest.raises(RuntimeError, match="unknown service"):
        install(tmp_path, source_root, "one", "stable")

    assert service.read_text() == "local service"


def test_uninstall_refuses_modified_web_service(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    service = tmp_path / "system/services/control_freak"
    service.write_text("modified")

    with pytest.raises(RuntimeError, match="Modified service file"):
        uninstall(tmp_path)

    assert service.read_text() == "modified"
    assert app(tmp_path).is_dir()


def test_repository_release_trust_matches_runtime_payload(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    destination = tmp_path / "release"
    destination.mkdir()
    spec = importlib.util.spec_from_file_location("repository_installer", source / "deploy/install_batocera.py")
    assert spec is not None and spec.loader is not None
    repository_installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repository_installer)

    manifest = repository_installer._release_source(source, destination, "repository-test")

    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == sorted(paths)


def test_release_source_rejects_symlink_to_external_file(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path, source_root)
    external = tmp_path / "external.py"
    external.write_text("outside")
    (source / "src/control_freak/external.py").symlink_to(external)

    with pytest.raises(RuntimeError, match="Release source contains a symlink"):
        install(tmp_path / "root", source, "one", "stable")

    assert not app(tmp_path / "root").exists()




def test_forged_manifest_cannot_delete_arbitrary_in_root_file(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    victim = tmp_path / "keep-me"
    victim.write_text("safe")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["releases"] = ["../../keep-me"]
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert victim.read_text() == "safe"
    assert app(tmp_path).is_dir()




def test_static_file_swap_to_symlink_during_validation_aborts(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    stable_file = app(tmp_path) / "image.png"
    victim = tmp_path / "same-content"
    victim.write_bytes(stable_file.read_bytes())
    real = installer.checksum
    swapped = False

    def race(path: Path) -> str:
        nonlocal swapped
        if path == stable_file and not swapped:
            swapped = True
            stable_file.unlink()
            stable_file.symlink_to(victim)
        return real(path)

    monkeypatch.setattr(installer, "checksum", race)
    with pytest.raises(RuntimeError, match="Modified stable file"):
        install(tmp_path, source_root, "two", "stable")
    assert stable_file.is_symlink()
    assert current(tmp_path)["release_id"] == "one"


def test_unknown_release_and_unknown_root_object_are_preserved(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    unknown_release = app(tmp_path) / "releases/mystery"
    unknown_release.mkdir()
    (unknown_release / "payload").write_text("mine")

    with pytest.raises(RuntimeError, match="Unknown release"):
        install(tmp_path, source_root, "two", "stable")
    assert (unknown_release / "payload").read_text() == "mine"

    shutil.rmtree(unknown_release)
    unknown = app(tmp_path) / "notes"
    unknown.write_text("mine")
    with pytest.raises(RuntimeError, match="Unknown object"):
        uninstall(tmp_path)
    assert unknown.read_text() == "mine"


def test_update_recovers_after_release_publish_crash(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.ROOT_MANIFEST:
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path)["release_id"] == "one"

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path)["release_id"] == "two"


def test_update_recovery_rejects_reused_id_with_different_trust(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copied_source(tmp_path / "source", source_root)
    root = tmp_path / "root"
    private = resign_source(source)
    install(root, source, "one", "stable")
    installed_service = root / installer.SERVICES_REL / installer.SERVICE_NAME
    before = installed_service.read_bytes()
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.ROOT_MANIFEST:
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(root, source, "two", "nightly")

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    recoveries = []
    monkeypatch.setattr(installer, "_recover_activation", lambda *_args: recoveries.append("activation"))
    monkeypatch.setattr(installer, "_recover_lifecycle", lambda *_args: recoveries.append("lifecycle"))
    published_signature = app(root) / "releases/two" / installer.TRUST_SIGNATURE
    original_signature = published_signature.read_bytes()
    published_signature.chmod(0o600)
    published_signature.write_bytes(b"corrupt")
    published_signature.chmod(0o444)
    with pytest.raises(RuntimeError, match="Published release differs from source"):
        install(root, source, "two", "nightly")
    assert installed_service.read_bytes() == before
    published_signature.chmod(0o600)
    published_signature.write_bytes(original_signature)
    published_signature.chmod(0o444)

    wrapper = source / "deploy/control_freak"
    wrapper.write_bytes(wrapper.read_bytes() + b"\n# different authenticated wrapper\n")
    resign_source(source, private)

    with pytest.raises(RuntimeError, match="Published release differs from source"):
        install(root, source, "two", "nightly")

    assert installed_service.read_bytes() == before
    assert recoveries == []


@pytest.mark.parametrize("enabled,running", [(False, False), (False, True), (True, False), (True, True)])
def test_update_journals_service_state_before_abrupt_pointer_crash(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    running: bool,
) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.CURRENT:
            raise SystemExit(9)
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_service_enabled", lambda _root: enabled)
    monkeypatch.setattr(installer, "_service_running", lambda _root: running)
    monkeypatch.setattr(installer, "_service_actions", lambda *_args: None)
    monkeypatch.setattr(installer, "_atomic_write_at", crash)

    with pytest.raises(SystemExit):
        install(tmp_path, source_root, "two", "nightly")

    journal = json.loads((app(tmp_path) / installer.ACTIVATION_JOURNAL).read_text())
    assert journal["version"] == 2
    assert journal["service_enabled"] is enabled
    assert journal["service_running"] is running








def test_same_id_source_mismatch_precedes_activation_recovery(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copied_source(tmp_path / "source", source_root)
    root = tmp_path / "root"
    private = resign_source(source)
    install(root, source, "one", "stable")
    selected = current(root)
    (app(root) / installer.ACTIVATION_JOURNAL).write_bytes(
        installer._json_bytes({"version": 1, "before": selected, "after": selected})
    )
    (source / "src/control_freak/web.py").write_text((source / "src/control_freak/web.py").read_text() + "\n# changed\n")
    resign_source(source, private)
    actions = []
    monkeypatch.setattr(installer, "_service_actions", lambda *_args: actions.append(_args))

    with pytest.raises(RuntimeError, match="Source differs from immutable published release"):
        install(root, source, "one", "stable")

    assert actions == []


def test_same_id_signature_mismatch_precedes_activation_recovery(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    install(root, source_root, "one", "stable")
    selected = current(root)
    (app(root) / installer.ACTIVATION_JOURNAL).write_bytes(
        installer._json_bytes({"version": 1, "before": selected, "after": selected})
    )
    signature = app(root) / "releases/one" / installer.TRUST_SIGNATURE
    signature.chmod(0o600)
    signature.write_bytes(b"corrupt")
    signature.chmod(0o444)
    actions = []
    monkeypatch.setattr(installer, "_service_actions", lambda *_args: actions.append(_args))

    with pytest.raises(RuntimeError, match="Source differs from immutable published release"):
        install(root, source_root, "one", "stable")

    assert actions == []


def test_new_id_source_authentication_precedes_activation_recovery(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copied_source(tmp_path / "source", source_root)
    root = tmp_path / "root"
    resign_source(source)
    install(root, source, "one", "stable")
    selected = current(root)
    (app(root) / installer.ACTIVATION_JOURNAL).write_bytes(
        installer._json_bytes({"version": 1, "before": selected, "after": selected})
    )
    (source / "src/control_freak/web.py").write_text((source / "src/control_freak/web.py").read_text() + "\n# unsigned\n")
    actions = []
    monkeypatch.setattr(installer, "_service_actions", lambda *_args: actions.append(_args))

    with pytest.raises(RuntimeError, match="Release trust statement does not match runtime payload"):
        install(root, source, "two", "nightly")

    assert actions == []


def test_state_preparation_rejects_symlinked_system_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "system").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        installer._prepare_state_directory(root)

    assert not (outside / "control-freak").exists()


def test_install_enforces_service_directory_boundary(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")

    services = tmp_path / installer.SERVICES_REL
    assert services.stat().st_uid == os.geteuid()
    assert services.stat().st_mode & 0o777 == 0o700


def test_fallback_stop_ignores_missing_and_preserves_unrelated_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "system/control-freak"
    state.mkdir(parents=True, mode=0o700)
    state.parent.chmod(0o755)
    state.chmod(0o700)
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **_kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))

    installer._stop_runtime_process(tmp_path)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (state / "web.pid").write_text(str(sleeper.pid))
        with pytest.raises(RuntimeError, match="Invalid Control Freak PID evidence"):
            installer._stop_runtime_process(tmp_path)
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=3)

    assert calls == []
    assert (state / "web.pid").read_text() == str(sleeper.pid)


def test_fallback_stop_rejects_wrong_process_uid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "system/control-freak"
    state.mkdir(parents=True, mode=0o700)
    state.parent.chmod(0o755)
    state.chmod(0o700)
    (state / "web.pid").write_text(str(os.getpid()))
    real_stat = Path.stat
    pidfd = os.open("/dev/null", os.O_RDONLY)

    def fake_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == Path(f"/proc/{os.getpid()}"):
            current = real_stat(path)
            return os.stat_result((current.st_mode, current.st_ino, current.st_dev, current.st_nlink, os.geteuid() + 1, current.st_gid, current.st_size, current.st_atime, current.st_mtime, current.st_ctime))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(installer.os, "pidfd_open", lambda _pid: pidfd)
    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(RuntimeError, match="Invalid Control Freak PID evidence"):
        installer._stop_runtime_process(tmp_path)

    assert (state / "web.pid").read_text() == str(os.getpid())
    with pytest.raises(OSError):
        os.fstat(pidfd)


def test_registration_uses_existing_pygame_system_and_service_boundary(tmp_path: Path, source_root: Path) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    pygame.chmod(0o755)
    native_gamelist = pygame / "gamelist.xml"
    native_gamelist.write_text(
        "<gameList><game><path>./rommbrowser/rommbrowser.pygame</path><name>RomM-o-Cera</name></game></gameList>"
    )
    native_gamelist.chmod(0o644)

    install(tmp_path, source_root, "one", "stable")

    assert ET.parse(tmp_path / installer.OVERLAY_REL).getroot().find("system") is None
    games = ET.parse(pygame / "gamelist.xml").getroot().findall("game")
    assert [(game.findtext("path"), game.findtext("name")) for game in games] == [
        ("./rommbrowser/rommbrowser.pygame", "RomM-o-Cera"),
        ("../../system/control-freak/app/control-freak.pygame", "Control Freak"),
    ]
    launcher = app(tmp_path) / "control-freak.pygame"
    assert launcher.read_text().splitlines() == [
        "import os",
        f'os.execv("{tmp_path}/system/services/control_freak", ["control_freak", "launch"])',
    ]


def test_modified_service_is_not_replaced(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    service = tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME
    service.write_text("unknown")

    with pytest.raises(RuntimeError, match="Modified service file"):
        install(tmp_path, source_root, "two", "stable")

    assert service.read_text() == "unknown"


def test_installer_rejects_service_owned_by_unexpected_uid(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    service = tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME
    real_lstat = Path.lstat

    def lstat(path: Path):
        info = real_lstat(path)
        if path == service:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(RuntimeError, match="Modified service file"):
        install(tmp_path, source_root, "two", "stable")


def test_unknown_service_directory_alias_is_not_managed(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    alias = tmp_path / installer.SERVICES_REL / "control_freak.pygame"
    alias.symlink_to("unknown")

    install(tmp_path, source_root, "two", "stable")
    assert os.readlink(alias) == "unknown"

    uninstall(tmp_path)
    assert os.readlink(alias) == "unknown"


def test_shutdown_rejects_unsafe_state_before_touching_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        installer,
        "_prepare_state_directory",
        lambda _root: (_ for _ in ()).throw(RuntimeError("Refusing unsafe Control Freak state directory")),
    )
    monkeypatch.setattr(installer.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe state opened")))

    with pytest.raises(RuntimeError, match="unsafe Control Freak state directory"):
        installer._shutdown_runtime(Path("/userdata"), "one", True)










def test_concurrent_updates_are_serialized(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer._source_manifest
    first_entered = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    errors: list[BaseException] = []

    def controlled(source: Path, release_id: str, parent: Path) -> tuple[Path, dict]:
        nonlocal calls
        with calls_lock:
            calls += 1
            ordinal = calls
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(5)
        return real(source, release_id, parent)

    def update(release_id: str, done: threading.Event | None = None) -> None:
        try:
            install(tmp_path, source_root, release_id, "nightly")
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done:
                done.set()

    monkeypatch.setattr(installer, "_source_manifest", controlled)
    first = threading.Thread(target=update, args=("two",))
    second = threading.Thread(target=update, args=("three", second_done))
    first.start()
    assert first_entered.wait(5)
    second.start()
    assert not second_done.wait(0.1)
    with calls_lock:
        assert calls == 1
    release_first.set()
    first.join(5)
    second.join(5)

    assert errors == []
    assert not first.is_alive() and not second.is_alive()
    manifest = json.loads((app(tmp_path) / "install-manifest.json").read_text())
    assert manifest["releases"] == ["one", "two", "three"]
    assert current(tmp_path)["release_id"] == "three"


def test_force_reselects_same_release_only_when_source_matches(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path, source_root)
    install(tmp_path / "root", source, "one", "stable")
    release = app(tmp_path / "root") / "releases/one"
    inode = release.stat().st_ino

    operations = install(tmp_path / "root", source, "one", "nightly", force=True)
    assert operations[0] == "reselect release one"
    assert release.stat().st_ino == inode
    assert current(tmp_path / "root")["channel"] == "nightly"

    (source / "src/control_freak/app.py").write_text("changed source")
    with pytest.raises(RuntimeError, match="Release trust statement"):
        install(tmp_path / "root", source, "one", "stable", force=True)


def test_same_release_rejects_authenticated_service_wrapper_change(
    tmp_path: Path, source_root: Path
) -> None:
    source = copied_source(tmp_path, source_root)
    root = tmp_path / "root"
    install(root, source, "one", "stable")
    installed_service = root / installer.SERVICES_REL / installer.SERVICE_NAME
    before = installed_service.read_bytes()

    wrapper = source / "deploy/control_freak"
    wrapper.write_bytes(wrapper.read_bytes() + b"\n# authenticated replacement\n")
    resign_source(source)

    with pytest.raises(RuntimeError, match="immutable published release"):
        install(root, source, "one", "stable")

    assert installed_service.read_bytes() == before


def test_force_does_not_overwrite_unknown_current_pointer(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    pointer.write_text(json.dumps({"version": 1, "release_id": "unknown", "channel": "stable"}))

    with pytest.raises(RuntimeError, match="Invalid current pointer"):
        install(tmp_path, source_root, "one", "nightly", force=True)
    assert json.loads(pointer.read_text())["release_id"] == "unknown"


def test_current_pointer_symlink_is_unknown_and_preserved(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    contents = pointer.read_bytes()
    pointer.unlink()
    unknown = tmp_path / "unknown-current"
    unknown.write_bytes(contents)
    pointer.symlink_to(unknown)

    with pytest.raises(RuntimeError, match="current pointer"):
        install(tmp_path, source_root, "one", "nightly", force=True)
    assert pointer.is_symlink()
    assert unknown.read_bytes() == contents


@pytest.mark.parametrize(
    "relative",
    [
        "assets/control-freak-logo-1.png",
        "assets/control-freak-logo-2.png",
        "assets/control-freak-logo-3.png",
        "assets/control-freak-logo-4.png",
        "assets/control-freak-logo-5.png",
        "assets/control-freak-logo-6.png",
        "deploy/install_batocera.py",
    ],
)
def test_update_replaces_owned_stable_artifacts(tmp_path: Path, source_root: Path, relative: str) -> None:
    source = copied_source(tmp_path, source_root)
    private = resign_source(source)
    root = tmp_path / "root"
    install(root, source, "one", "stable")
    changed = source / relative
    changed.write_bytes(changed.read_bytes() + b"\nchanged protocol\n")
    resign_source(source, private)

    install(root, source, "two", "nightly")

    destination = next(name for name, path in installer._required(source).items() if path == changed)
    assert (app(root) / destination).read_bytes() == changed.read_bytes()
    assert current(root) == {"channel": "nightly", "release_id": "two", "version": 1}
    assert (app(root) / "releases/two").exists()
    assert registered(root)


def test_source_public_key_must_match_installer_pin(tmp_path: Path, source_root: Path) -> None:
    expected = installer.RELEASE_PUBLIC_KEY
    source = copied_source(tmp_path / "source", source_root)
    resign_source(source)
    installer.RELEASE_PUBLIC_KEY = expected

    with pytest.raises(RuntimeError, match="release public key"):
        install(tmp_path / "root", source, "one", "stable")


def test_signed_statement_covers_installer_and_registration_image(source_root: Path) -> None:
    statement = json.loads((source_root / "deploy/release-trust.json").read_text())
    covered = {item["path"] for item in statement["source"]}
    assert "deploy/install_batocera.py" in covered
    assert "assets/control-freak-image.png" in covered


def test_uninstall_retry_rolls_forward_after_partial_registration_removal(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    real_unlink = installer.os.unlink
    crashed = False

    def crash(name, *args, **kwargs):
        nonlocal crashed
        if name == "gamelist.xml" and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(installer.os, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        uninstall(tmp_path)
    monkeypatch.setattr(installer.os, "unlink", real_unlink)

    uninstall(tmp_path)
    assert not app(tmp_path).exists()
    assert all(not path.exists() for path in installer._registration_paths(tmp_path).values())
    assert not list((tmp_path / installer.APP_PARENT_REL).glob(".control-freak-remove-*"))


def test_install_recovers_interrupted_uninstall_after_quarantine_deletion(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    installer._fresh_activation(tmp_path, True)
    real_rmtree = installer.shutil.rmtree
    crashed = False

    def crash_after_delete(*args, **kwargs):
        nonlocal crashed
        real_rmtree(*args, **kwargs)
        if not crashed:
            crashed = True
            raise KeyboardInterrupt

    monkeypatch.setattr(installer.shutil, "rmtree", crash_after_delete)
    with pytest.raises(KeyboardInterrupt):
        uninstall(tmp_path)
    monkeypatch.setattr(installer.shutil, "rmtree", real_rmtree)

    install(tmp_path, source_root, "two", "stable")
    assert current(tmp_path)["release_id"] == "two"
    state = tmp_path / "system/control-freak"
    assert not (state / installer.UNINSTALL_INTENT).exists()
    assert not (state / installer.UNINSTALL_JOURNAL).exists()
    assert not (state / installer.INSTALL_ACTIVATION).exists()


def test_uninstall_retry_recovers_after_quarantine_deletion(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    installer._fresh_activation(tmp_path, True)
    real_rmtree = installer.shutil.rmtree

    def crash_after_delete(*args, **kwargs):
        real_rmtree(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer.shutil, "rmtree", crash_after_delete)
    with pytest.raises(KeyboardInterrupt):
        uninstall(tmp_path)
    monkeypatch.setattr(installer.shutil, "rmtree", real_rmtree)

    uninstall(tmp_path)
    state = tmp_path / "system/control-freak"
    assert not (state / installer.UNINSTALL_INTENT).exists()
    assert not (state / installer.UNINSTALL_JOURNAL).exists()
    assert not (state / installer.INSTALL_ACTIVATION).exists()


@pytest.mark.parametrize("recover_with_install", [False, True])
def test_uninstall_recovers_after_partial_quarantine_deletion(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    recover_with_install: bool,
) -> None:
    install(tmp_path, source_root, "one", "stable")
    real_rmtree = installer.shutil.rmtree

    def crash_after_partial_delete(name, *, dir_fd, **kwargs):
        quarantine_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            os.unlink(installer.CURRENT, dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
        finally:
            os.close(quarantine_fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer.shutil, "rmtree", crash_after_partial_delete)
    with pytest.raises(KeyboardInterrupt):
        uninstall(tmp_path)
    monkeypatch.setattr(installer.shutil, "rmtree", real_rmtree)

    if recover_with_install:
        install(tmp_path, source_root, "two", "stable")
        assert current(tmp_path)["release_id"] == "two"
    else:
        uninstall(tmp_path)
        assert not app(tmp_path).exists()
    state = tmp_path / "system/control-freak"
    assert not (state / installer.UNINSTALL_INTENT).exists()
    assert not list((tmp_path / installer.APP_PARENT_REL).glob(".control-freak-remove-*"))


def test_uninstall_intent_refuses_replacement_active_app(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    manifest, _current = installer._load_install(application, allow_missing_service=True)
    original_info = application.stat()
    quarantine = ".control-freak-remove-" + "0" * 24
    installer._uninstall_intent(tmp_path, quarantine, manifest, original_info)
    preserved = application.parent / "preserved-original"
    application.rename(preserved)
    shutil.copytree(preserved, application)
    shutdown_called = False

    def unexpected_shutdown(*_args, **_kwargs):
        nonlocal shutdown_called
        shutdown_called = True
        raise AssertionError("replacement app reached runtime shutdown")

    monkeypatch.setattr(installer, "_shutdown_runtime", unexpected_shutdown)

    with pytest.raises(RuntimeError, match="Ambiguous uninstall"):
        uninstall(tmp_path)

    assert not shutdown_called
    assert application.is_dir()
    assert preserved.is_dir()
    assert application.stat().st_ino != original_info.st_ino


def test_update_rejects_changed_registration_image_before_mutation(
    tmp_path: Path, source_root: Path
) -> None:
    private = resign_source(source_root)
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    before_manifest = (application / installer.ROOT_MANIFEST).read_bytes()
    before_current = (application / installer.CURRENT).read_bytes()
    before_releases = sorted(path.name for path in (application / "releases").iterdir())
    source = copied_source(tmp_path / "changed-image", source_root)
    (source / "assets/control-freak-image.png").write_bytes(b"new signed registration image")
    resign_source(source, private)

    with pytest.raises(RuntimeError, match="registration image"):
        install(tmp_path, source, "two", "stable")

    assert (application / installer.ROOT_MANIFEST).read_bytes() == before_manifest
    assert (application / installer.CURRENT).read_bytes() == before_current
    assert sorted(path.name for path in (application / "releases").iterdir()) == before_releases
    installer._load_install(application, allow_missing_service=True)


def test_runtime_dependencies_reject_unsupported_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.sys, "version_info", (3, 15, 0))

    with pytest.raises(RuntimeError, match="Python"):
        installer._runtime_dependencies(tmp_path)


def test_install_rolls_forward_authenticated_uninstall_quarantine(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    real_unlink = installer.os.unlink

    def crash(name, *args, **kwargs):
        if name == "gamelist.xml":
            raise KeyboardInterrupt
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(installer.os, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        uninstall(tmp_path)
    monkeypatch.setattr(installer.os, "unlink", real_unlink)

    install(tmp_path, source_root, "two", "stable")
    assert current(tmp_path)["release_id"] == "two"
    assert not list((tmp_path / installer.APP_PARENT_REL).glob(".control-freak-remove-*"))


def test_nfs_group_write_mode_normalization_is_accepted(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    root = app(tmp_path)
    for name in installer.STATIC_FILES:
        expected = installer.STATIC_MODES[name]
        (root / name).chmod(expected | 0o020)

    operations = install(tmp_path, source_root, "one", "stable", force=True)

    assert operations[0] == "reselect release one"
    for name in installer.STATIC_FILES:
        assert (root / name).stat().st_mode & 0o777 == installer.STATIC_MODES[name]


def test_invalid_release_source_does_not_mutate_stable_files(
    tmp_path: Path, source_root: Path
) -> None:
    install(tmp_path, source_root, "one", "stable")
    application = app(tmp_path)
    before = {name: (application / name).read_bytes() for name in installer.STATIC_FILES}
    before_manifest = (application / installer.ROOT_MANIFEST).read_bytes()
    source = copied_source(tmp_path, source_root)
    (source / "assets/control-freak-logo-4.png").write_bytes(b"rejected replacement")
    runtime = source / "src/control_freak/app.py"
    runtime.write_bytes(runtime.read_bytes() + b"\n# unsigned change\n")

    with pytest.raises(RuntimeError, match="Release trust statement does not match runtime payload"):
        install(tmp_path, source, "one", "stable", force=True)

    assert {name: (application / name).read_bytes() for name in installer.STATIC_FILES} == before
    assert (application / installer.ROOT_MANIFEST).read_bytes() == before_manifest


def test_update_refuses_changed_stable_file_mode(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    installer_file = app(tmp_path) / "install_batocera.py"
    installer_file.chmod(0o755)

    with pytest.raises(RuntimeError, match="Modified stable file: install_batocera.py"):
        install(tmp_path, source_root, "two", "nightly")

    assert current(tmp_path) == {"channel": "stable", "release_id": "one", "version": 1}
    assert installer_file.stat().st_mode & 0o777 == 0o755
    assert not (app(tmp_path) / "releases/two").exists()


def test_upgrade_tracks_old_releases_and_uninstall_removes_all(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    image_hash = installer.checksum(app(tmp_path) / "image.png")
    install(tmp_path, source_root, "two", "nightly")

    manifest = json.loads((app(tmp_path) / "install-manifest.json").read_text())
    assert manifest["releases"] == ["one", "two"]
    assert (app(tmp_path) / "releases/one").is_dir()
    assert installer.checksum(app(tmp_path) / "image.png") == image_hash
    assert uninstall(tmp_path) == []
    assert not app(tmp_path).exists()
    assert not registered(tmp_path)






def test_uninstall_drift_makes_zero_changes(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    changed = app(tmp_path) / "releases/one/control_freak/app.py"
    changed.write_text("local change")

    with pytest.raises(RuntimeError, match="Modified release file"):
        uninstall(tmp_path)

    assert changed.read_text() == "local change"
    assert (app(tmp_path) / "install-manifest.json").is_file()
    assert registered(tmp_path)








def test_uninstall_runtime_failure_restores_registration(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("runtime shutdown failed")

    monkeypatch.setattr(installer, "_shutdown_runtime", fail)
    with pytest.raises(RuntimeError, match="runtime shutdown failed"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert (tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME).is_file()
    assert registered(tmp_path)


def test_uninstall_service_removal_failure_rolls_back_publication(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, source_root, "one", "stable")
    real_unlink = installer.os.unlink

    def fail_service(name, *args, **kwargs):
        if name == installer.SERVICE_NAME:
            raise OSError("service removal failed")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(installer.os, "unlink", fail_service)
    with pytest.raises(OSError, match="service removal failed"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert (tmp_path / installer.SERVICES_REL / installer.SERVICE_NAME).is_file()
    assert registered(tmp_path)


def test_shutdown_journal_restores_exact_runtime_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "system/control-freak"
    state.mkdir(parents=True)
    previous_web = b'{"version":1,"enabled":true,"tailscale":false,"token":"token"}\n'
    previous_audio = b'{"version":1,"blocked":["sink"],"managed":["sink"]}\n'
    (state / "web.json").write_text('{"version":1,"enabled":false,"tailscale":false,"token":""}\n')
    (state / "audio.json").write_text('{"version":1,"blocked":[],"managed":[]}\n')
    (state / installer.UNINSTALL_JOURNAL).write_text(json.dumps({
        "version": 2,
        "web": previous_web.hex(),
        "audio": previous_audio.hex(),
        "audio_captured": True,
        "service_enabled": False,
        "service_running": False,
    }))
    restored = []
    monkeypatch.setattr(installer, "_restore_service_state", lambda _root, enabled, running: restored.append((enabled, running)))

    installer._recover_shutdown_journal(tmp_path, True)

    assert (state / "web.json").read_bytes() == previous_web
    assert (state / "audio.json").read_bytes() == previous_audio
    assert restored == [(False, False)]
    assert not (state / installer.UNINSTALL_JOURNAL).exists()






def test_relative_root_has_working_pointer(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    install(Path("userdata"), source_root, "one", "stable")

    pointer = current(tmp_path / "userdata")
    release = app(tmp_path / "userdata") / "releases" / pointer["release_id"]
    assert release.is_dir()
    assert not (app(tmp_path / "userdata") / "current").exists()


def test_existing_root_through_symlinked_ancestor_is_refused(tmp_path: Path, source_root: Path) -> None:
    real = tmp_path / "real"
    (real / "userdata").mkdir(parents=True)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlinked userdata path"):
        install(link / "userdata", source_root, "one", "stable")

    assert not app(real / "userdata").exists()


def test_manifest_schema_requires_exact_types(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["version"] = True
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_current_schema_requires_exact_types(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    value = json.loads(pointer.read_text())
    value["version"] = True
    pointer.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid current pointer"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_manifest_rejects_non_string_release_without_type_error(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["releases"] = [{}]
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_install_refuses_unknown_launcher_even_with_force(tmp_path: Path, source_root: Path) -> None:
    launcher = app(tmp_path) / "control-freak.pygame"
    launcher.parent.mkdir(parents=True)
    (tmp_path / "system/control-freak").chmod(0o700)
    launcher.write_text("unknown local launcher\n")
    with pytest.raises(RuntimeError, match="unsafe Control Freak state directory"):
        install(tmp_path, source_root, "one", "stable", force=True)


def test_install_dry_run_does_not_write(tmp_path: Path, source_root: Path) -> None:
    operations = install(tmp_path, source_root, "one", "stable", dry_run=True)
    assert operations[0] == "install release one"
    assert list(tmp_path.iterdir()) == []
