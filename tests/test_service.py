from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time


TEST_PRIVATE_KEY = subprocess.run(
    ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
    capture_output=True,
    check=True,
).stdout
TEST_PUBLIC_KEY = subprocess.run(
    ["openssl", "pkey", "-pubout"], input=TEST_PRIVATE_KEY, capture_output=True, check=True
).stdout


def sign_statement(statement: bytes) -> bytes:
    descriptor = os.memfd_create("test-release-key")
    try:
        os.write(descriptor, TEST_PRIVATE_KEY)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f"/proc/self/fd/{descriptor}"],
            input=statement,
            pass_fds=(descriptor,),
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.close(descriptor)


def wrapper_content(app: Path, state: Path) -> str:
    return (
        (Path(__file__).parents[1] / "deploy/control_freak").read_text()
        .replace("import fcntl", "import fcntl\nfor _name, _value in {'F_ADD_SEALS': 1033, 'F_SEAL_SEAL': 1, 'F_SEAL_SHRINK': 2, 'F_SEAL_GROW': 4, 'F_SEAL_WRITE': 8}.items(): setattr(fcntl, _name, getattr(fcntl, _name, _value))")
        .replace('PUBLIC_KEY_PEM = b"__CONTROL_FREAK_RELEASE_PUBLIC_KEY__"', f"PUBLIC_KEY_PEM = {TEST_PUBLIC_KEY!r}")
        .replace('APP = Path("/userdata/system/control-freak/app")', f"APP = Path({str(app)!r})")
        .replace('STATE = Path("/userdata/system/control-freak")', f"STATE = Path({str(state)!r})")
        .replace("/usr/bin/python3", sys.executable)
    )


def build_app(app: Path, release_id: str, web: str) -> None:
    release = app / "releases" / release_id / "control_freak"
    release.mkdir(parents=True)
    files = {
        "__init__.py": "",
        "app.py": "def run(): pass\n",
        "web.py": web,
        "service.py": (Path(__file__).parents[1] / "src/control_freak/service.py").read_text(),
    }
    entries = []
    for name, content in files.items():
        target = release / name
        target.write_text(content)
        target.chmod(0o644)
        entries.append({"path": f"control_freak/{name}", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "mode": 0o644})
    statement = (json.dumps({
        "version": 2,
        "files": entries,
        "service": {"sha256": "0" * 64, "mode": 0o755},
        "source": [
            {"path": "deploy/install_batocera.py", "sha256": hashlib.sha256(b"").hexdigest(), "mode": 0o644},
            {"path": "assets/control-freak-image.png", "sha256": hashlib.sha256(b"").hexdigest(), "mode": 0o644},
        ],
    }, indent=2, sort_keys=True) + "\n").encode()
    (release.parent / "trust.json").write_bytes(statement)
    (release.parent / "trust.sig").write_bytes(sign_statement(statement))
    (release.parent / "trust.json").chmod(0o444)
    (release.parent / "trust.sig").chmod(0o444)
    (release.parent / "manifest.json").write_text(json.dumps({"version": 1, "release_id": release_id, "files": entries}))
    modes = {"image.png": 0o644, **{f"logo-proposal-{number}.png": 0o644 for number in range(1, 7)}, "install_batocera.py": 0o644}
    static = {}
    for name, mode in modes.items():
        target = app / name
        target.write_bytes(b"")
        target.chmod(mode)
        static[name] = hashlib.sha256(b"").hexdigest()
    (app / "current.json").write_text(json.dumps({"version": 1, "release_id": release_id, "channel": "stable"}))
    userdata = app.parents[2]
    library = userdata / "system/control-freak/library"
    registration_paths = {
        "overlay": userdata / "system/configs/emulationstation/es_systems_control_freak.cfg",
        "marker": library / "Control Freak.controlfreak",
        "gamelist": library / "gamelist.xml",
        "image": library / "images/control-freak.png",
    }
    registration = {}
    for name, target in registration_paths.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        target.chmod(0o644)
        registration[name] = {"sha256": hashlib.sha256(b"").hexdigest(), "mode": 0o644}
    (app / "install-manifest.json").write_text(json.dumps({"version": 6, "static": static, "service": {"name": "control_freak", "sha256": "0" * 64, "mode": 0o755}, "registration": registration, "releases": [release_id]}))


def bind_wrapper(app: Path, script: Path) -> None:
    manifest_path = app / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["service"]["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    release = app / "releases" / json.loads((app / "current.json").read_text())["release_id"]
    trust = json.loads((release / "trust.json").read_text())
    canonical = script.read_bytes().replace(
        f"APP = Path({str(app)!r})".encode(), b'APP = Path("/userdata/system/control-freak/app")'
    ).replace(
        f"STATE = Path({str(script.parents[1] / 'control-freak')!r})".encode(), b'STATE = Path("/userdata/system/control-freak")'
    )
    trust["service"]["sha256"] = hashlib.sha256(canonical).hexdigest()
    statement = (json.dumps(trust, indent=2, sort_keys=True) + "\n").encode()
    for name in ("trust.json", "trust.sig"):
        (release / name).chmod(0o644)
    (release / "trust.json").write_bytes(statement)
    (release / "trust.sig").write_bytes(sign_statement(statement))
    for name in ("trust.json", "trust.sig"):
        (release / name).chmod(0o444)


def make_wrapper(tmp_path: Path, web: str) -> tuple[Path, Path, Path]:
    app = tmp_path / "system/control-freak/app"
    state = tmp_path / "system/control-freak"
    build_app(app, "one", web)
    state.chmod(0o700)
    script = tmp_path / "system/services/control_freak"
    script.parent.mkdir(parents=True)
    script.write_text(wrapper_content(app, state))
    script.chmod(0o755)
    bind_wrapper(app, script)
    return app, state, script


def test_service_uses_start_stop_daemon_cycle(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "import time\ndef main(): time.sleep(30)\n")
    try:
        assert subprocess.run([script, "start"], check=False).returncode == 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and subprocess.run([script, "status"], check=False).returncode:
            time.sleep(0.05)
        assert subprocess.run([script, "status"], check=False).returncode == 0
        assert (state / "web.pid").read_text().strip().isdigit()
        assert not (state / "web.ready").exists()
    finally:
        subprocess.run([script, "stop"], check=False)
    assert not (state / "web.pid").exists()


def test_service_refuses_modified_release_before_start(tmp_path: Path) -> None:
    app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    (app / "releases/one/control_freak/web.py").write_text("def main(): raise SystemExit\n")
    assert subprocess.run([script, "start"], check=False, capture_output=True).returncode != 0
    assert not (state / "web.pid").exists()


def test_service_wrapper_rejects_unexpected_owner_uid(tmp_path: Path) -> None:
    _app, _state, script = make_wrapper(tmp_path, "def main(): pass\n")
    probe = (
        "import os,runpy,sys;from pathlib import Path;from types import SimpleNamespace;"
        "script=Path(sys.argv[1]);real=Path.lstat;"
        "Path.lstat=lambda self: SimpleNamespace(st_mode=real(self).st_mode,st_uid=os.geteuid()+1) if self==script else real(self);"
        "sys.argv=[str(script),'launch'];runpy.run_path(script,run_name='__main__')"
    )

    result = subprocess.run([sys.executable, "-c", probe, str(script)], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Modified Control Freak service wrapper" in result.stderr


def test_service_start_fails_when_worker_exits_during_activation(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")

    assert subprocess.run([script, "start"], check=False).returncode != 0
    assert subprocess.run([script, "status"], check=False).returncode != 0
    assert not (state / "web.pid").exists()


def test_status_and_stop_ignore_reused_unrelated_python_pid(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (state / "web.pid").write_text(str(sleeper.pid))
        assert subprocess.run([script, "status"], check=False).returncode != 0
        assert subprocess.run([script, "stop"], check=False).returncode != 0
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=3)


def test_service_uses_exact_executable_matching(tmp_path: Path) -> None:
    _app, _state, script = make_wrapper(tmp_path, "def main(): pass\n")
    source = script.read_text()
    assert '"-x", PYTHON' in source
    assert "os.pidfd_open" in source
    assert "signal.pidfd_send_signal" in source


def test_owned_pid_rejects_non_root_process_owner(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    probe = (
        "import os,runpy,sys;from types import SimpleNamespace;"
        "ns=runpy.run_path(sys.argv[1],run_name='probe');"
        "ns['PIDFILE'].write_text(str(os.getpid()));"
        "ns['COMMAND']=(sys.executable,'-c','pass');ns['PYTHON']=sys.executable;"
        "ns['Path'].read_bytes=lambda self: b'bad';"
        "real_stat=ns['os'].stat;ns['os'].stat=lambda path,*a,**k: SimpleNamespace(st_uid=1) if str(path).startswith('/proc/') else real_stat(path,*a,**k);"
        "ns['owned_pid']()"
    )
    result = subprocess.run([sys.executable, "-c", probe, str(script)], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Invalid Control Freak PID evidence" in result.stderr


def test_launch_refuses_runtime_lockout(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    (state / "runtime.lockout").write_text('{"version": 1}\n')
    result = subprocess.run([script, "launch"], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "locked out" in result.stderr


def test_start_refuses_runtime_lockout(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "import time\ndef main(): time.sleep(30)\n")
    (state / "runtime.lockout").write_text('{"version": 1}\n')

    result = subprocess.run([script, "start"], check=False, capture_output=True, text=True)

    assert result.returncode != 0
    assert "locked out" in result.stderr
    assert not (state / "web.pid").exists()


def test_direct_service_action_waits_for_lifecycle_lock(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    lock_fd = os.open(state / ".control-freak.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    process = subprocess.Popen([script, "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.1)
        assert process.poll() is None
        os.close(lock_fd)
        assert process.wait(timeout=3) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_service_manager_action_reuses_inherited_lifecycle_lock(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    lock_fd = os.open(state / ".control-freak.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    environment = os.environ.copy()
    environment["CONTROL_FREAK_LIFECYCLE_FD"] = str(lock_fd)
    try:
        result = subprocess.run(
            [script, "status"], env=environment, pass_fds=(lock_fd,), check=False, timeout=3,
        )
    finally:
        os.close(lock_fd)

    assert result.returncode == 1


def test_direct_worker_invocation_is_rejected(tmp_path: Path) -> None:
    _app, _state, script = make_wrapper(tmp_path, "def main(): pass\n")
    environment = os.environ.copy()
    environment["CONTROL_FREAK_WORKER"] = "1"

    result = subprocess.run([script, "worker"], check=False, capture_output=True, text=True, env=environment)

    assert result.returncode != 0
    assert "Unauthorized Control Freak worker invocation" in result.stderr


def test_forged_worker_pid_cannot_bypass_runtime_lockout(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    (state / "runtime.lockout").write_text('{"version": 1}\n')
    probe = (
        "import os,sys;script=sys.argv[1];pidfile=sys.argv[2];"
        "open(pidfile,'w').write(str(os.getpid()));"
        "env=os.environ.copy();env['CONTROL_FREAK_WORKER']='1';"
        "os.execve(script,[script,'worker'],env)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(script), str(state / "web.pid")],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "locked out" in result.stderr


def test_launch_allows_child_state_mutation_without_deadlock(tmp_path: Path) -> None:
    module = (
        "from pathlib import Path\n"
        "def main():\n"
        " from control_freak.stateio import locked\n"
        " with locked(Path(__file__).parents[2] / 'web.json'): pass\n"
    )
    _app, _state, script = make_wrapper(tmp_path, module)
    result = subprocess.run([script, "launch"], check=False, timeout=3)
    assert result.returncode == 0


def test_start_preserves_malformed_pid_evidence(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "import time\ndef main(): time.sleep(30)\n")
    pidfile = state / "web.pid"
    pidfile.write_text("not-a-pid\n")

    result = subprocess.run([script, "start"], check=False, capture_output=True, text=True)

    assert result.returncode != 0
    assert pidfile.read_text() == "not-a-pid\n"


def test_service_rejects_unsafe_state_directory(tmp_path: Path) -> None:
    _app, state, script = make_wrapper(tmp_path, "def main(): pass\n")
    state.chmod(0o777)
    assert subprocess.run([script, "status"], check=False, capture_output=True).returncode != 0


def test_service_state_path_is_opened_component_by_component() -> None:
    source = (Path(__file__).parents[1] / "deploy/control_freak").read_text()
    assert 'os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)' in source
    assert "dir_fd=directory_fd" in source
    assert "STATE.mkdir(" not in source


def test_foreground_worker_has_no_pid_or_readiness_protocol() -> None:
    source = (Path(__file__).parents[1] / "src/control_freak/service.py").read_text()
    assert "web.pid" not in source
    assert "web.ready" not in source
    assert "CONTROL_FREAK_LAUNCH_NONCE" not in source
