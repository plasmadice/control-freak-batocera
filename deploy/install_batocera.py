#!/usr/bin/env python3
"""Crash-safe Control Freak installer for Batocera userdata."""
from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from xml.parsers import expat
from pathlib import Path, PurePosixPath

APP_REL = Path("system/control-freak/app")
APP_PARENT_REL = Path("system/control-freak")
LEGACY_APP_REL = Path("roms/pygame/control-freak")
LEGACY_APP_PARENT_REL = Path("roms/pygame")
NATIVE_LAUNCHER_REL = Path("roms/pygame/control-freak-launcher/control-freak.pygame")
LIBRARY_REL = Path("system/control-freak/library")
OVERLAY_REL = Path("system/configs/emulationstation/es_systems_control_freak.cfg")
GAME_PATH = "./Control Freak.controlfreak"
PYGAME_GAME_PATH = "./control-freak-launcher/control-freak.pygame"
LEGACY_APP_GAME_PATH = "./control-freak/control-freak.pygame"
LEGACY_GAME_PATH = "../../system/services/control_freak.pygame"
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
V6_STATIC_FILES = (
    "image.png",
    "logo-proposal-1.png",
    "logo-proposal-2.png",
    "logo-proposal-3.png",
    "logo-proposal-4.png",
    "logo-proposal-5.png",
    "logo-proposal-6.png",
    "install_batocera.py",
)
STATIC_FILES = ("control-freak.pygame", *V6_STATIC_FILES)
STATIC_MODES = {
    "control-freak.pygame": 0o644,
    "image.png": 0o644,
    "logo-proposal-1.png": 0o644,
    "logo-proposal-2.png": 0o644,
    "logo-proposal-3.png": 0o644,
    "logo-proposal-4.png": 0o644,
    "logo-proposal-5.png": 0o644,
    "logo-proposal-6.png": 0o644,
    "install_batocera.py": 0o644,
}
V6_STATIC_MODES = {name: STATIC_MODES[name] for name in V6_STATIC_FILES}
LEGACY_LAUNCHER = "control-freak.pygame"
LEGACY_STATIC_FILES = (LEGACY_LAUNCHER, *V6_STATIC_FILES)
LEGACY_STATIC_MODES = {LEGACY_LAUNCHER: 0o755, **V6_STATIC_MODES}
ROOT_MANIFEST = "install-manifest.json"
LIFECYCLE_JOURNAL = ".lifecycle-update.json"
ACTIVATION_JOURNAL = ".activation.json"
CURRENT = "current.json"
RELEASE_MANIFEST = "manifest.json"
TRUST_STATEMENT = "trust.json"
TRUST_SIGNATURE = "trust.sig"
QUARANTINE = re.compile(r"^\.control-freak-remove-[0-9a-f]{24}$")
LOCK_FILE = ".control-freak.lock"
_ACTIVE_LIFECYCLE_LOCK: int | None = None
OS_RELEASE_PATH = Path("/etc/os-release")
SERVICES_REL = Path("system/services")
SERVICE_NAME = "control_freak"
SERVICE_MODE = 0o755
UNINSTALL_JOURNAL = "uninstall-runtime.json"
UNINSTALL_INTENT = "uninstall-intent.json"
RUNTIME_LOCK = "web.lock"
RUNTIME_LOCKOUT = "runtime.lockout"
INSTALL_ACTIVATION = "install-activation.json"
SERVICE_CODE = "import os,runpy,sys;sys.path.insert(0,'/proc/self/fd/'+os.environ['CONTROL_FREAK_BUNDLE_FD']);runpy.run_module('control_freak.service',run_name='__main__')"
SYSTEMS_OVERLAY = """<?xml version="1.0"?>
<systemList/>
"""
LOCAL_SYSTEMS_OVERLAY = """<?xml version="1.0"?>
<systemList>
  <system>
    <name>controlfreak</name>
    <fullname>Control Freak</fullname>
    <path>/userdata/system/control-freak/library</path>
    <extension>.controlfreak</extension>
    <command>/userdata/system/services/control_freak launch</command>
    <theme>pygame</theme>
  </system>
</systemList>
"""
LAUNCHER_CODE = 'import os\nos.execv("/userdata/system/services/control_freak", ["control_freak", "launch"])\n'
MARKER_NAME = "Control Freak.controlfreak"
REGISTRATION_MODES = {"overlay": 0o644, "marker": 0o644, "gamelist": 0o644, "image": 0o644, "launcher": 0o644}
RELEASE_PUBLIC_KEY = base64.b64decode(
    b"LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQm9qQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FZOEFNSUlCaWdLQ0FZRUEyTVEzMnp5bzRUU3hOeldrWXFSdwpYa1ZsbW1Hb2ltSWtFVWw2QjU4aVpCQ3M0Nk5OM0ltUTB1MEQweGNvNUhiRjVwMm5DKzlZWm5FeXB2YnhocVl1CnB5aUlqNE05YVJLZXJKT010c20vMGRsY3RpaGlyV0NCQWRwSTNUMFJEeTdFR3lwNDVRbDJkOWFBM3RqYmUyMGwKWUVIM1R2SXJnNXFPTmovSXFScjdjdGNLdW1TZTdLdFNwQXlFQUN6UjJ5cmhIeVZsMXlCOFJPWjJXMndSTW14bApMaEVnbDgzeXk3NU44SXI2bURNK1BLeTRtOHlWTHAwWjlabUVYYStHTnM2L0p1UTVRYVBWMWYwVXI3TU1hSC9kCnJEWEEvZ1YzQmx0RFNyUnlBeXpuTU9wdENCVitzVHBpMnBoZzhDS0EzMnZEMHYzZXlKRzg5UVdTS3NhU0FMVnAKdHdLN1V4eVV5YVlNcGsza3VmNVNWZFovMGRpbUFKZklzTWdQaTUxbk5oZFdwcW5xRWlJVXFtaVd5bTloK2tkTAp0MXQrYVpKNS9MUkVLUHB2a3hBd0Q0ZVJuelhvZTlmMmhuSlkvWXVoZkxUYTlHOGp1bGpISWkyRmREaFpQQmhDCkZTYklTZkNhb0dqeU0rdHBiK1Nodk1rNnNTcHpkdG9zR2QwYkNEY2Nkc3g1QWdNQkFBRT0KLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)
TRUSTED_SOURCE_FILES = {
    "assets/control-freak-image.png": 0o644,
    **{f"assets/control-freak-logo-{number}.png": 0o644 for number in range(1, 7)},
    "deploy/control_freak": 0o755,
    "deploy/control-freak.pygame": 0o644,
    "deploy/install_batocera.py": 0o644,
    "deploy/release-public.pem": 0o644,
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _safe_root(userdata: Path) -> Path:
    root = userdata.absolute()
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"Refusing symlinked userdata path: {cursor}")
        if not cursor.exists():
            break
    return root


def _require_supported_batocera(path: Path | None = None) -> None:
    try:
        data = (OS_RELEASE_PATH if path is None else path).read_text()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Direct install requires Batocera 43 or 44") from exc
    if len(data.encode()) > 65536:
        raise RuntimeError("Direct install requires Batocera 43 or 44")
    values = {}
    try:
        for line in data.splitlines():
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
                raise ValueError
            parsed = shlex.split(raw, comments=False, posix=True)
            if len(parsed) != 1 and not (not parsed and raw == ""):
                raise ValueError
            values[key] = parsed[0] if parsed else ""
    except ValueError as exc:
        raise RuntimeError("Direct install requires Batocera 43 or 44") from exc
    version = values.get("VERSION", "")
    if values.get("NAME") != "Batocera.linux" or values.get("ID") != "buildroot" or not re.fullmatch(r"(?:43|44)(?:\.[0-9]+)*", version):
        raise RuntimeError("Direct install requires Batocera 43 or 44")


def _mkdirs_no_symlinks(root: Path, relative: Path) -> None:
    cursor = root
    cursor.mkdir(exist_ok=True)
    for part in relative.parts:
        cursor /= part
        try:
            cursor.mkdir()
            cursor.chmod(0o755)
        except FileExistsError:
            if cursor.is_symlink() or not cursor.is_dir():
                raise RuntimeError(f"Refusing unsafe install parent: {cursor}")


def _launcher_boundary(root: Path, app: Path) -> None:
    if not root.exists():
        return
    cursor = root
    paths = [root]
    for part in app.relative_to(root).parts:
        cursor /= part
        if not cursor.exists():
            break
        paths.append(cursor)
    managed = (app / ROOT_MANIFEST).is_file() and not (app / ROOT_MANIFEST).is_symlink()
    for path in paths:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or managed and path == app and info.st_mode & 0o022:
            raise RuntimeError("Refusing unsafe launcher trust boundary")


def _open_dir(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/" if path.is_absolute() else ".", flags)
    try:
        for part in path.parts[1:] if path.is_absolute() else path.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise RuntimeError(f"Refusing unsafe directory: {path}")
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_owned_dir(path: Path) -> int:
    fd = _open_dir(path)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise RuntimeError("Refusing unsafe launcher trust boundary")
    return fd


def _open_shared_pygame_dir(path: Path) -> int:
    fd = _open_dir(path)
    if os.fstat(fd).st_mode & 0o002:
        os.close(fd)
        raise RuntimeError("Refusing unsafe pygame directory")
    return fd


def _open_native_pygame_dir(path: Path) -> int:
    fd = _open_shared_pygame_dir(path)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise RuntimeError("Refusing unsafe native pygame directory")
    return fd


def _open_native_launcher_dir(root: Path, *, create: bool = False) -> int:
    pygame_fd = _open_native_pygame_dir(root / LEGACY_APP_PARENT_REL)
    try:
        try:
            launcher_fd = os.open("control-freak-launcher", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pygame_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir("control-freak-launcher", 0o755, dir_fd=pygame_fd)
            os.fsync(pygame_fd)
            launcher_fd = os.open("control-freak-launcher", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pygame_fd)
        info = os.fstat(launcher_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(launcher_fd)
            raise RuntimeError("Refusing unsafe native launcher directory")
        return launcher_fd
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError("Refusing unsafe native launcher directory") from exc
    finally:
        os.close(pygame_fd)


def _lifecycle_lock(directory_fd: int) -> int:
    global _ACTIVE_LIFECYCLE_LOCK
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    lock_fd = os.open(LOCK_FILE, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Invalid lifecycle lock")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _ACTIVE_LIFECYCLE_LOCK = lock_fd
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _read_at(directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Expected regular file: {name}")
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(), info
    finally:
        os.close(fd)


def _read_fd(fd: int) -> tuple[bytes, os.stat_result]:
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as handle:
        data = handle.read()
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Expected regular file")
    return data, info


def _stage_write_at(directory_fd: int, name: str, data: bytes, mode: int) -> str:
    temporary = f".{name}.tmp-{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, mode, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    return temporary


def _commit_write_at(directory_fd: int, temporary: str, name: str) -> None:
    try:
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_at(directory_fd: int, name: str, data: bytes, mode: int) -> None:
    _commit_write_at(directory_fd, _stage_write_at(directory_fd, name, data, mode), name)


def _atomic_create_at(directory_fd: int, name: str, data: bytes, mode: int) -> None:
    temporary = _stage_write_at(directory_fd, name, data, mode)
    try:
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise RuntimeError(f"Refusing existing file: {name}") from exc
    finally:
        os.unlink(temporary, dir_fd=directory_fd)


def _exchange_paths_at(directory_fd: int, left: str, right: str) -> None:
    """Atomically exchange two directory entries without a clobber gap."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "Atomic path exchange is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(directory_fd, os.fsencode(left), directory_fd, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), right)
    os.fsync(directory_fd)


def _gamelist_exchange_supported(root: Path) -> bool:
    """Probe the exact shared directory without touching platform data."""
    pygame = root / LEGACY_APP_PARENT_REL
    pygame_existed = pygame.exists()
    roms_existed = (root / "roms").exists()
    _mkdirs_no_symlinks(root, LEGACY_APP_PARENT_REL)
    pygame_fd = _open_shared_pygame_dir(pygame)
    names = [f".control-freak-exchange-{secrets.token_hex(12)}" for _ in range(2)]
    try:
        for name in names:
            _atomic_create_at(pygame_fd, name, b"", 0o600)
        try:
            _exchange_paths_at(pygame_fd, names[0], names[1])
        except OSError as exc:
            if exc.errno in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
                return False
            raise
        _exchange_paths_at(pygame_fd, names[0], names[1])
        return True
    finally:
        for name in names:
            try:
                os.unlink(name, dir_fd=pygame_fd)
            except FileNotFoundError:
                pass
        os.fsync(pygame_fd)
        os.close(pygame_fd)
        if not pygame_existed:
            try:
                pygame.rmdir()
            except OSError:
                pass
        if not roms_existed:
            try:
                (root / "roms").rmdir()
            except OSError:
                pass


def _native_registration_supported(root: Path, *, probe_exchange: bool = True) -> bool:
    pygame = root / LEGACY_APP_PARENT_REL
    if not pygame.exists() and not pygame.is_symlink():
        return _gamelist_exchange_supported(root) if probe_exchange else False
    pygame_fd = _open_shared_pygame_dir(pygame)
    try:
        info = os.fstat(pygame_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o020:
            return False
        try:
            launcher_info = os.stat("control-freak-launcher", dir_fd=pygame_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(launcher_info.st_mode) or launcher_info.st_uid != os.geteuid() or launcher_info.st_mode & 0o022:
                raise RuntimeError("Refusing unsafe native launcher directory")
            launcher_fd = _open_native_launcher_dir(root)
            try:
                try:
                    file_info = os.stat("control-freak.pygame", dir_fd=launcher_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or file_info.st_mode & 0o777 != 0o644:
                        raise RuntimeError("Refusing unsafe native launcher file")
            finally:
                os.close(launcher_fd)
    finally:
        os.close(pygame_fd)
    return _gamelist_exchange_supported(root) if probe_exchange else True


def _registration_strategy(root: Path, app: Path) -> str:
    if (app / ROOT_MANIFEST).is_file() and not (app / ROOT_MANIFEST).is_symlink():
        manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
        if not _valid_install_manifest(manifest):
            raise RuntimeError("Invalid install manifest")
        if manifest["version"] in {8, 9}:
            strategy = manifest["registration_strategy"]
            if strategy == "native" and not _native_registration_supported(root, probe_exchange=False):
                raise RuntimeError("Native registration requires an owned non-writable pygame directory")
            return strategy
        if manifest["version"] == 7:
            if not _native_registration_supported(root):
                raise RuntimeError("Native registration requires atomic path exchange")
            return "native"
        if manifest["version"] == 6:
            return "native" if _native_registration_supported(root) else "local"
    return "native" if _native_registration_supported(root) else "local"


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Refusing symlink in staged tree: {path}")
        if path.is_file():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif path.is_dir():
            directories.append(path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fd = _open_dir(directory)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _strict_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise RuntimeError("Invalid release manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("Invalid release manifest path")
    return path


def _release_trust(source: Path, files: list[dict]) -> tuple[bytes, bytes]:
    statement_path = source / "deploy/release-trust.json"
    signature_path = source / "deploy/release-trust.sig"
    public_key = source / "deploy/release-public.pem"
    required = (statement_path, signature_path, public_key)
    if any(path.is_symlink() or not path.is_file() for path in required):
        raise FileNotFoundError("release trust material is incomplete")
    if public_key.read_bytes() != RELEASE_PUBLIC_KEY:
        raise RuntimeError("Source release public key does not match installer pin")
    statement = statement_path.read_bytes()
    signature = signature_path.read_bytes()
    try:
        parsed = json.loads(statement)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid release trust statement") from exc
    service = _service_bytes(source, Path("/userdata"))
    source_files = [
        {"path": relative, "sha256": checksum(source / relative), "mode": mode}
        for relative, mode in TRUSTED_SOURCE_FILES.items()
    ]
    expected = {
        "version": 2,
        "files": files,
        "service": {"sha256": hashlib.sha256(service).hexdigest(), "mode": SERVICE_MODE},
        "source": source_files,
    }
    if parsed != expected or statement != _json_bytes(expected) or not signature:
        raise RuntimeError("Release trust statement does not match runtime payload")
    key_fd = os.memfd_create("control-freak-installer-key")
    try:
        os.write(key_fd, RELEASE_PUBLIC_KEY)
        os.lseek(key_fd, 0, os.SEEK_SET)
        result = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-verify", f"/proc/self/fd/{key_fd}", "-signature", str(signature_path)],
            input=statement, pass_fds=(key_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    finally:
        os.close(key_fd)
    if result.returncode:
        raise RuntimeError("Invalid release trust signature")
    return statement, signature


def _release_source(source: Path, destination: Path, release_id: str) -> dict:
    package = source / "src/control_freak"
    if not package.is_dir():
        raise FileNotFoundError(package)
    if package.is_symlink() or any(path.is_symlink() for path in package.rglob("*")):
        raise RuntimeError("Release source contains a symlink")
    shutil.copytree(package, destination / "control_freak", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = []
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Release source contains a symlink")
        if path.is_file():
            os.chmod(path, 0o644)
            relative = path.relative_to(destination).as_posix()
            files.append({"path": relative, "sha256": checksum(path), "mode": path.stat().st_mode & 0o777})
    statement, signature = _release_trust(source, files)
    (destination / TRUST_STATEMENT).write_bytes(statement)
    (destination / TRUST_SIGNATURE).write_bytes(signature)
    os.chmod(destination / TRUST_STATEMENT, 0o444)
    os.chmod(destination / TRUST_SIGNATURE, 0o444)
    manifest = {"version": 1, "release_id": release_id, "files": files}
    (destination / RELEASE_MANIFEST).write_bytes(_json_bytes(manifest))
    os.chmod(destination / RELEASE_MANIFEST, 0o444)
    _fsync_tree(destination)
    return manifest


def _load_json_file(path: Path, label: str) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid {label}")
    return value


def _validate_release(release: Path, release_id: str) -> dict:
    if release.is_symlink() or not release.is_dir():
        raise RuntimeError(f"Invalid release: {release_id}")
    manifest = _load_json_file(release / RELEASE_MANIFEST, "release manifest")
    if (
        set(manifest) != {"version", "release_id", "files"}
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or type(manifest.get("release_id")) is not str
        or manifest["release_id"] != release_id
        or not isinstance(manifest.get("files"), list)
    ):
        raise RuntimeError("Invalid release manifest")
    expected = {RELEASE_MANIFEST, TRUST_STATEMENT, TRUST_SIGNATURE}
    expected_directories: set[str] = set()
    seen: set[PurePosixPath] = set()
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "mode"}
            or type(item.get("sha256")) is not str
            or not SHA256.fullmatch(item["sha256"])
            or type(item.get("mode")) is not int
            or not 0 <= item["mode"] <= 0o777
        ):
            raise RuntimeError("Invalid release manifest entry")
        relative = _strict_relative(item["path"])
        if relative in seen or relative.name == RELEASE_MANIFEST:
            raise RuntimeError("Invalid release manifest entry")
        seen.add(relative)
        target = release.joinpath(*relative.parts)
        try:
            matches = not target.is_symlink() and target.is_file() and checksum(target) == item["sha256"] and target.stat().st_mode & 0o777 == item["mode"]
        except OSError:
            matches = False
        if not matches:
            raise RuntimeError(f"Modified release file: {release_id}/{relative}")
        expected.add(relative.as_posix())
        expected_directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != ".")
    actual = set()
    actual_directories = set()
    for path in release.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Unknown release object: {path.relative_to(release)}")
        if path.is_file():
            actual.add(path.relative_to(release).as_posix())
        elif path.is_dir():
            actual_directories.add(path.relative_to(release).as_posix())
        else:
            raise RuntimeError(f"Unknown release object: {path.relative_to(release)}")
    if actual != expected or actual_directories != expected_directories:
        raise RuntimeError(f"Unknown release objects in {release_id}")
    return manifest


def _source_manifest(source: Path, release_id: str, parent: Path) -> tuple[Path, dict]:
    staging = Path(tempfile.mkdtemp(prefix=".release-source-", dir=parent))
    return staging, _release_source(source, staging, release_id)


def _snapshot_source(source: Path, parent: Path) -> Path:
    snapshot = Path(tempfile.mkdtemp(prefix=".source-snapshot-", dir=parent))
    paths = list(TRUSTED_SOURCE_FILES) + [
        "deploy/release-trust.json", "deploy/release-trust.sig",
    ]
    package = source / "src/control_freak"
    if package.is_symlink() or not package.is_dir():
        raise FileNotFoundError("release source is incomplete")
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Release source contains a symlink")
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts:
            paths.append((Path("src/control_freak") / path.relative_to(package)).as_posix())
    try:
        for relative in paths:
            source_path = source / relative
            if source_path.is_symlink() or not source_path.is_file():
                raise FileNotFoundError("release source is incomplete")
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source_path.read_bytes())
            destination.chmod(0o700 if relative == "deploy/control_freak" else 0o600)
        _fsync_tree(snapshot)
        return snapshot
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _same_manifest(left: dict, right: dict) -> bool:
    return left == right


def _valid_install_manifest(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    static = manifest.get("static")
    service = manifest.get("service")
    releases = manifest.get("releases")
    version = manifest.get("version")
    service_version = type(version) is int and version in {3, 4, 5, 6, 7, 8, 9}
    expected_static = STATIC_FILES if version in {7, 8, 9} else V6_STATIC_FILES if version in {4, 6} else LEGACY_STATIC_FILES
    registration = manifest.get("registration")
    strategy = manifest.get("registration_strategy")
    expected_keys = {"version", "static", "service", "releases"} | ({"registration"} if version in {6, 7, 8, 9} else set()) | ({"registration_strategy"} if version in {8, 9} else set())
    return not (
        type(version) is not int
        or version not in {2, 3, 4, 5, 6, 7, 8, 9}
        or set(manifest) != (expected_keys if service_version else {"version", "static", "releases"})
        or not isinstance(static, dict)
        or set(static) != set(expected_static)
        or any(type(value) is not str or not SHA256.fullmatch(value) for value in static.values())
        or not isinstance(releases, list)
        or any(type(item) is not str or not RELEASE_ID.fullmatch(item) for item in releases)
        or len(releases) != len(set(releases))
        or (
            service_version
            and (
                not isinstance(service, dict)
                or set(service) != {"name", "sha256", "mode"}
                or service.get("name") != SERVICE_NAME
                or type(service.get("sha256")) is not str
                or not SHA256.fullmatch(service["sha256"])
                or type(service.get("mode")) is not int
                or service["mode"] != SERVICE_MODE
            )
        )
        or version in {8, 9} and strategy not in {"native", "local"}
        or version in {6, 7, 8, 9} and (
            not isinstance(registration, dict)
            or set(registration) != (set(REGISTRATION_MODES) if version == 9 and strategy == "native" else set(REGISTRATION_MODES) - {"launcher"})
            or any(
                not isinstance(item, dict)
                or set(item) != {"sha256", "mode"}
                or type(item.get("sha256")) is not str
                or not SHA256.fullmatch(item["sha256"])
                or item.get("mode") != REGISTRATION_MODES[name]
                for name, item in registration.items()
            )
        )
    )


def _registration_paths(root: Path, strategy: str = "native") -> dict[str, Path]:
    library = root / LIBRARY_REL
    paths = {
        "overlay": root / OVERLAY_REL,
        "marker": library / MARKER_NAME,
        "gamelist": library / "gamelist.xml",
        "image": library / "images/control-freak.png",
    }
    if strategy == "native":
        paths["launcher"] = root / NATIVE_LAUNCHER_REL
    return paths


def _library_gamelist() -> bytes:
    root = ET.Element("gameList")
    game = ET.SubElement(root, "game")
    for key, value in {
        "path": GAME_PATH,
        "name": "Control Freak",
        "desc": "Controller-first settings and management for Batocera.",
        "genre": "Utility",
        "image": "./images/control-freak.png",
        "developer": "Haku",
        "publisher": "Haku",
        "players": "1",
    }.items():
        ET.SubElement(game, key).text = value
    ET.indent(root, space="\t")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _registration_bytes(root: Path, source: Path, strategy: str = "native") -> dict[str, bytes]:
    image = source / "assets/control-freak-image.png"
    if image.is_symlink() or not image.is_file():
        raise FileNotFoundError("release source is incomplete")
    overlay = SYSTEMS_OVERLAY if strategy == "native" else LOCAL_SYSTEMS_OVERLAY
    data = {"overlay": overlay.encode(), "marker": b"", "gamelist": _library_gamelist(), "image": image.read_bytes()}
    if strategy == "native":
        launcher = (source / "deploy/control-freak.pygame").read_bytes()
        if launcher != LAUNCHER_CODE.encode():
            raise RuntimeError("Invalid Control Freak launcher")
        data["launcher"] = launcher if root == Path("/userdata") else launcher.replace(b"/userdata", str(root).encode())
    return data


def _installed_registration_bytes(app: Path, strategy: str) -> dict[str, bytes]:
    overlay = SYSTEMS_OVERLAY if strategy == "native" else LOCAL_SYSTEMS_OVERLAY
    data = {"overlay": overlay.encode(), "marker": b"", "gamelist": _library_gamelist(), "image": (app / "image.png").read_bytes()}
    if strategy == "native":
        data["launcher"] = (app / "control-freak.pygame").read_bytes()
    return data


def _preflight_registration(root: Path, data: dict[str, bytes]) -> list[str]:
    missing = []
    for name, path in _registration_paths(root, "native" if "launcher" in data else "local").items():
        if name == "launcher":
            try:
                directory_fd = _open_native_launcher_dir(root)
                try:
                    current, info = _read_at(directory_fd, path.name)
                finally:
                    os.close(directory_fd)
            except FileNotFoundError:
                missing.append(name)
                continue
            if current != data[name] or info.st_uid != os.geteuid() or info.st_mode & 0o777 != REGISTRATION_MODES[name]:
                raise RuntimeError(f"Refusing unknown registration file: {name}")
            continue
        try:
            info = path.lstat()
            if path.is_symlink():
                raise OSError(f"Refusing registration symlink: {name}")
            current = path.read_bytes() if stat.S_ISREG(info.st_mode) and not path.is_symlink() else None
        except FileNotFoundError:
            missing.append(name)
            continue
        if current != data[name] or info.st_uid != os.geteuid() or info.st_mode & 0o777 != REGISTRATION_MODES[name]:
            raise RuntimeError(f"Refusing unknown registration file: {name}")
    return missing


def _gamelist_is_owned(path: Path) -> bool:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return False
    if root.tag != "gameList" or root.attrib or (root.text or "").strip() or (root.tail or "").strip() or len(root) != 1:
        return False
    game = root[0]
    if game.tag != "game" or game.attrib or (game.text or "").strip() or (game.tail or "").strip():
        return False
    values: dict[str, str | None] = {}
    for child in game:
        if not isinstance(child.tag, str) or child.attrib or len(child) or child.tag in values or (child.tail or "").strip():
            return False
        values[child.tag] = child.text
    expected_game = ET.fromstring(_library_gamelist()).find("game")
    assert expected_game is not None
    expected = {child.tag: child.text for child in expected_game}
    mutable = {"playcount", "lastplayed", "gametime", "lang"}
    return all(values.get(tag) == value for tag, value in expected.items()) and set(values) <= set(expected) | mutable


def _validate_registration(root: Path, registration: dict, expected: dict[str, bytes] | None = None, *, allow_absent: bool = False) -> None:
    for name, path in _registration_paths(root, "native" if "launcher" in registration else "local").items():
        if name == "launcher":
            try:
                directory_fd = _open_native_launcher_dir(root)
                try:
                    data, info = _read_at(directory_fd, path.name)
                finally:
                    os.close(directory_fd)
                matches = (
                    info.st_uid == os.geteuid()
                    and info.st_mode & 0o777 == registration[name]["mode"]
                    and hashlib.sha256(data).hexdigest() == registration[name]["sha256"]
                )
            except FileNotFoundError:
                if allow_absent:
                    continue
                matches = False
            except OSError:
                matches = False
            if not matches:
                raise RuntimeError(f"Modified registration file: {name}")
            if expected is not None and registration[name]["sha256"] != hashlib.sha256(expected[name]).hexdigest():
                raise RuntimeError(f"Lifecycle protocol mismatch: {name}")
            continue
        try:
            info = path.lstat()
            matches = (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_mode & 0o777 == registration[name]["mode"]
                and (checksum(path) == registration[name]["sha256"] or name == "gamelist" and _gamelist_is_owned(path))
            )
        except FileNotFoundError:
            if allow_absent:
                continue
            matches = False
        except OSError:
            matches = False
        if not matches:
            raise RuntimeError(f"Modified registration file: {name}")
        if expected is not None and registration[name]["sha256"] != hashlib.sha256(expected[name]).hexdigest():
            raise RuntimeError(f"Lifecycle protocol mismatch: {name}")


def _ensure_registration(root: Path, source: Path, strategy: str) -> dict:
    data = _registration_bytes(root, source, strategy)
    desired = {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "mode": REGISTRATION_MODES[name]}
        for name, payload in data.items()
    }
    app = root / APP_REL
    previous = None
    if (app / ROOT_MANIFEST).is_file() and not (app / ROOT_MANIFEST).is_symlink():
        manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
        if manifest.get("version") in {6, 7, 8, 9} and isinstance(manifest.get("registration"), dict):
            previous = manifest["registration"]
            _validate_registration(root, previous)
            if previous["image"]["sha256"] != hashlib.sha256(data["image"]).hexdigest():
                raise RuntimeError("Signed registration image differs from installed registration image")
            if set(previous) == set(desired):
                return desired
    paths = _registration_paths(root, strategy)
    missing = _preflight_registration(root, data)
    _prepare_owned_directory(root, OVERLAY_REL.parent)
    _prepare_owned_directory(root, LIBRARY_REL)
    _prepare_owned_directory(root, LIBRARY_REL / "images")
    if strategy == "native":
        _mkdirs_no_symlinks(root, LEGACY_APP_PARENT_REL)
        launcher_fd = _open_native_launcher_dir(root, create=True)
        os.close(launcher_fd)
    created = []
    try:
        for name in missing:
            path = paths[name]
            directory_fd = _open_native_launcher_dir(root) if name == "launcher" else _open_dir(path.parent)
            try:
                _atomic_create_at(directory_fd, path.name, data[name], REGISTRATION_MODES[name])
                created.append(path)
            finally:
                os.close(directory_fd)
    except BaseException:
        for path in reversed(created):
            directory_fd = _open_native_launcher_dir(root) if path == paths.get("launcher") else _open_dir(path.parent)
            try:
                os.unlink(path.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        raise
    return desired


def _update_registration(root: Path, before: dict, after: dict, data: dict[str, bytes]) -> None:
    for name, path in _registration_paths(root, "native" if "launcher" in after else "local").items():
        if after[name]["sha256"] != hashlib.sha256(data[name]).hexdigest():
            raise RuntimeError(f"Lifecycle protocol mismatch: {name}")
        if name == "launcher":
            directory_fd = _open_native_launcher_dir(root)
            try:
                current, info = _read_at(directory_fd, path.name)
            finally:
                os.close(directory_fd)
            digest = hashlib.sha256(current).hexdigest()
        else:
            info = path.lstat()
            digest = checksum(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != after[name]["mode"]:
            raise RuntimeError(f"Modified registration file: {name}")
        if digest == after[name]["sha256"]:
            continue
        if name == "gamelist" and before[name]["sha256"] == after[name]["sha256"] and _gamelist_is_owned(path):
            continue
        if digest not in {before[name]["sha256"], after[name]["sha256"]}:
            raise RuntimeError(f"Modified registration file: {name}")
        directory_fd = _open_native_launcher_dir(root) if name == "launcher" else _open_dir(path.parent)
        try:
            _atomic_write_at(directory_fd, path.name, data[name], after[name]["mode"])
        finally:
            os.close(directory_fd)


def _remove_registration(root: Path, registration: dict, *, allow_absent: bool = False) -> None:
    _validate_registration(root, registration, allow_absent=allow_absent)
    for path in _registration_paths(root, "native" if "launcher" in registration else "local").values():
        if not path.exists():
            continue
        directory_fd = _open_native_launcher_dir(root) if path == root / NATIVE_LAUNCHER_REL else _open_dir(path.parent)
        try:
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    for directory in (root / NATIVE_LAUNCHER_REL.parent, root / LIBRARY_REL / "images", root / LIBRARY_REL):
        try:
            directory.rmdir()
        except OSError:
            pass


def _load_install(
    app: Path,
    expected_static: dict[str, bytes] | None = None,
    expected_service: bytes | None = None,
    allow_missing_service: bool = False,
    allow_activation_journal: bool = False,
    allow_missing_registration: bool = False,
) -> tuple[dict, dict]:
    manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
    if not _valid_install_manifest(manifest):
        raise RuntimeError("Invalid install manifest")
    static = manifest["static"]
    service = manifest.get("service")
    releases = manifest["releases"]
    version = manifest["version"]
    modes = STATIC_MODES if version in {7, 8, 9} else V6_STATIC_MODES if version in {4, 6} else LEGACY_STATIC_MODES
    for name in static:
        target = app / name
        try:
            matches = (
                not target.is_symlink()
                and target.is_file()
                and checksum(target) == manifest["static"][name]
                and (target.stat().st_mode & 0o777) in {modes[name], modes[name] | 0o020}
            )
        except OSError:
            matches = False
        if not matches:
            raise RuntimeError(f"Modified stable file: {name}")
        if expected_static is not None and name in expected_static and manifest["static"][name] != hashlib.sha256(expected_static[name]).hexdigest():
            raise RuntimeError(f"Lifecycle protocol mismatch: {name}")
    service_path = app.parents[2] / SERVICES_REL / SERVICE_NAME

    try:
        service_info = service_path.lstat()
        service_absent = False
        service_hash = checksum(service_path) if stat.S_ISREG(service_info.st_mode) and service_info.st_mode & 0o777 == SERVICE_MODE and service_info.st_uid == os.geteuid() else None
    except FileNotFoundError:
        service_absent = True
        service_hash = None
    except OSError:
        service_absent = False
        service_hash = None
    service_record = service if isinstance(service, dict) else {}
    allowed_service_hashes = {service_record["sha256"]} if version in {3, 4, 5, 6, 7, 8, 9} else set()
    if service_hash not in allowed_service_hashes and not (service_absent and (allow_missing_service or version == 2)):
        raise RuntimeError("Modified service file")
    if expected_service is not None and (version not in {3, 4, 5, 6, 7, 8, 9} or service_record["sha256"] != hashlib.sha256(expected_service).hexdigest()):
        raise RuntimeError("Lifecycle protocol mismatch: service")
    if version in {6, 7, 8, 9}:
        _validate_registration(app.parents[2], manifest["registration"], allow_absent=allow_missing_registration)
    current = _load_json_file(app / CURRENT, "current pointer")
    if (
        set(current) != {"version", "release_id", "channel"}
        or type(current.get("version")) is not int
        or current["version"] != 1
        or type(current.get("release_id")) is not str
        or current["release_id"] not in releases
        or type(current.get("channel")) is not str
        or current["channel"] not in {"stable", "nightly"}
    ):
        raise RuntimeError("Invalid current pointer")
    actual = set(os.listdir(app))
    expected = set(static) | {ROOT_MANIFEST, CURRENT, "releases"}
    if allow_activation_journal:
        expected.add(ACTIVATION_JOURNAL)
    if actual != expected or (app / "releases").is_symlink() or not (app / "releases").is_dir():
        raise RuntimeError("Unknown object in application root")
    release_names = set(os.listdir(app / "releases"))
    unknown = release_names - set(releases)
    if unknown:
        raise RuntimeError(f"Unknown release: {sorted(unknown)[0]}")
    if release_names != set(releases):
        raise RuntimeError("Tracked release is missing")
    for release_id in releases:
        _validate_release(app / "releases" / release_id, release_id)
    return manifest, current


def _valid_pointer(value: object, releases: list[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"version", "release_id", "channel"}
        and type(value.get("version")) is int
        and value["version"] == 1
        and type(value.get("release_id")) is str
        and value["release_id"] in releases
        and type(value.get("channel")) is str
        and value["channel"] in {"stable", "nightly"}
    )


def _recover_activation(root: Path, app: Path) -> None:
    journal_path = app / ACTIVATION_JOURNAL
    if not journal_path.exists():
        return
    manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
    journal = _load_json_file(journal_path, "activation journal")
    releases = manifest.get("releases") if _valid_install_manifest(manifest) else None
    journal_version = journal.get("version")
    journal_keys = {"version", "before", "after"} | ({"service_enabled", "service_running"} if journal_version == 2 else set())
    if (
        not isinstance(releases, list)
        or set(journal) != journal_keys
        or type(journal_version) is not int
        or journal_version not in {1, 2}
        or not _valid_pointer(journal.get("before"), releases)
        or not _valid_pointer(journal.get("after"), releases)
        or journal_version == 2 and (
            type(journal.get("service_enabled")) is not bool
            or type(journal.get("service_running")) is not bool
        )
    ):
        raise RuntimeError("Invalid activation journal")
    current = _load_json_file(app / CURRENT, "current pointer")
    if current not in (journal["before"], journal["after"]):
        raise RuntimeError("Activation journal does not match current pointer")
    validated_manifest, validated_current = _load_install(app, allow_activation_journal=True)
    if validated_manifest != manifest or validated_current != current:
        raise RuntimeError("Activation journal changed during validation")
    app_fd = _open_dir(app)
    try:
        version = manifest["version"]
        modes = STATIC_MODES if version in {7, 8, 9} else V6_STATIC_MODES if version in {4, 6} else LEGACY_STATIC_MODES
        for name, mode in modes.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=app_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 not in {mode, mode | 0o020}:
                    raise RuntimeError(f"Modified stable file: {name}")
                if info.st_mode & 0o777 != mode:
                    os.fchmod(fd, mode)
            finally:
                os.close(fd)
        os.fsync(app_fd)
    finally:
        os.close(app_fd)
    validated_manifest, validated_current = _load_install(app, allow_activation_journal=True)
    if validated_manifest != manifest or validated_current != current:
        raise RuntimeError("Activation journal changed during normalization")
    _prepare_state_directory(root)
    app_fd = _open_dir(app)
    try:
        if journal_version == 2:
            _restore_service_state(root, journal["service_enabled"], journal["service_running"])
        elif _service_enabled(root):
            _service_actions(root, "stop", "start")
        os.unlink(ACTIVATION_JOURNAL, dir_fd=app_fd)
        os.fsync(app_fd)
    finally:
        os.close(app_fd)


def _document_parts(data: bytes) -> tuple[bytes, bytes, bytes]:
    parser = expat.ParserCreate()
    depth = 0
    root_start: int | None = None
    root_end: int | None = None

    def start(_name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth, root_start
        if depth == 0:
            root_start = parser.CurrentByteIndex
        depth += 1

    def end(_name: str) -> None:
        nonlocal depth, root_end
        depth -= 1
        if depth == 0:
            index = parser.CurrentByteIndex
            if data[index : index + 2] == b"</":
                close = data.find(b">", index)
                if close < 0:
                    raise RuntimeError("Cannot safely edit gamelist.xml")
                root_end = close + 1
            else:
                root_end = index

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise RuntimeError("Cannot safely edit gamelist.xml") from exc
    if root_start is None or root_end is None:
        raise RuntimeError("Cannot safely edit gamelist.xml")
    return data[:root_start], data[root_start:root_end], data[root_end:]


def _normalize_xml_parts(prefix: bytes, document: bytes, suffix: bytes) -> tuple[bytes, str, bytes]:
    declaration = re.search(
        br'<\?xml\b[^>]*\bencoding\s*=\s*["\']([A-Za-z0-9._:-]+)["\'][^>]*\?>',
        prefix,
        re.IGNORECASE,
    )
    encoding = declaration.group(1).decode("ascii") if declaration else "utf-8"
    if encoding.lower().replace("_", "-").startswith(("utf-16", "utf-32")):
        raise RuntimeError("Cannot safely edit gamelist.xml")
    try:
        prefix_text = prefix.decode(encoding)
        document_text = document.decode(encoding)
        suffix_text = suffix.decode(encoding)
    except (LookupError, UnicodeError) as exc:
        raise RuntimeError("Cannot safely edit gamelist.xml") from exc
    prefix_text = re.sub(
        r'(<\?xml\b[^>]*\bencoding\s*=\s*)(["\'])[^"\']+\2',
        lambda match: match.group(1) + '"utf-8"',
        prefix_text,
        count=1,
        flags=re.IGNORECASE,
    )
    return prefix_text.encode("utf-8"), document_text, suffix_text.encode("utf-8")


def gamelist_bytes(data: bytes | None, release_id: str, remove: bool = False, *, legacy_only: bool = False) -> bytes:
    prefix = b'<?xml version="1.0" encoding="utf-8"?>\n'
    suffix = b"\n"
    if data is not None:
        prefix, document, suffix = _document_parts(data)
        prefix, document_text, suffix = _normalize_xml_parts(prefix, document, suffix)
        try:
            root = ET.fromstring(document_text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
        except ET.ParseError as exc:
            raise RuntimeError("Cannot safely edit gamelist.xml") from exc
        if root.tag != "gameList":
            raise RuntimeError("Unexpected gamelist.xml root")
    else:
        root = ET.Element("gameList")
    owned_paths = {LEGACY_APP_GAME_PATH, LEGACY_GAME_PATH} if legacy_only else {PYGAME_GAME_PATH, LEGACY_APP_GAME_PATH, LEGACY_GAME_PATH}
    matches = [
        game
        for game in root.findall("game")
        if any(child.tag == "path" and child.text in owned_paths for child in game)
    ]
    if not legacy_only:
        fixed = {
            "path": PYGAME_GAME_PATH,
            "name": "Control Freak",
            "desc": "Controller-first settings and management for Batocera.",
            "genre": "Utility",
            "image": "../../system/control-freak/app/image.png",
            "developer": "Haku",
            "publisher": "Haku",
            "players": "1",
        }
        for game in (
            item for item in matches
            if any(child.tag == "path" and child.text == PYGAME_GAME_PATH for child in item)
        ):
            fields: dict[str, str | None] = {}
            malformed = bool(game.attrib or (game.text or "").strip() or (game.tail or "").strip())
            for child in game:
                if not isinstance(child.tag, str) or child.tag in fields or child.attrib or len(child) or (child.tail or "").strip():
                    malformed = True
                    break
                fields[child.tag] = child.text
            if malformed or any(fields.get(name) != value for name, value in fixed.items()) or set(fields) - set(fixed) - {"playcount", "lastplayed", "gametime", "lang"}:
                raise RuntimeError("Malformed Control Freak gamelist entry")
    if len(matches) > 1 or matches and matches[0].findtext("name") != "Control Freak":
        raise RuntimeError("Refusing to replace an unknown gamelist entry")
    if remove and not matches:
        assert data is not None
        return data
    if remove:
        root.remove(matches[0])
    else:
        game = matches[0] if matches else ET.SubElement(root, "game")
        for key, value in {
            "path": PYGAME_GAME_PATH,
            "name": "Control Freak",
            "desc": "Controller-first settings and management for Batocera.",
            "genre": "Utility",
            "image": "../../system/control-freak/app/image.png",
            "developer": "Haku",
            "publisher": "Haku",
            "players": "1",
        }.items():
            element = game.find(key)
            if element is None:
                element = ET.SubElement(game, key)
            element.text = value
    ET.indent(root, space="\t")
    return prefix + ET.tostring(root, encoding="utf-8") + suffix


def _gamelist_state(pygame_fd: int) -> tuple[bytes | None, int, os.stat_result | None]:
    try:
        data, info = _read_at(pygame_fd, "gamelist.xml")
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002:
            raise RuntimeError("Refusing unsafe gamelist.xml")
        return data, info.st_mode & 0o777, info
    except FileNotFoundError:
        return None, 0o644, None


def _same_file(left: os.stat_result | None, right: os.stat_result | None) -> bool:
    if left is None or right is None:
        return left is right
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _retain_path_at(directory_fd: int, name: str, recovery_base: str = "gamelist.xml") -> str:
    """Give a shared inode an independent durable recovery name."""
    recovery = f".{recovery_base}.tmp-{secrets.token_hex(12)}"
    os.link(name, recovery, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
    os.fsync(directory_fd)
    return recovery


def _replace_gamelist(pygame_fd: int, original: os.stat_result | None, original_data: bytes | None, data: bytes, mode: int) -> None:
    if original is None:
        _atomic_create_at(pygame_fd, "gamelist.xml", data, mode)
        return
    original_fd = os.open("gamelist.xml", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pygame_fd)
    try:
        held_data, held_info = _read_fd(original_fd)
        if not _same_file(original, held_info) or held_data != original_data:
            raise RuntimeError("gamelist.xml changed during operation")
        _retain_path_at(pygame_fd, "gamelist.xml")
        temporary = _stage_write_at(pygame_fd, "gamelist.xml", data, mode)
        _retain_path_at(pygame_fd, temporary)
        staged_info = os.stat(temporary, dir_fd=pygame_fd, follow_symlinks=False)
        exchanged = False
        try:
            current = os.stat("gamelist.xml", dir_fd=pygame_fd, follow_symlinks=False)
            if not _same_file(original, current):
                raise RuntimeError("gamelist.xml changed during operation")
            try:
                _exchange_paths_at(pygame_fd, temporary, "gamelist.xml")
                exchanged = True
            except BaseException:
                live_info = os.stat("gamelist.xml", dir_fd=pygame_fd, follow_symlinks=False)
                exchanged = _same_inode(staged_info, live_info)
                raise
            displaced, displaced_info = _read_at(pygame_fd, temporary)
            held_data, held_info = _read_fd(original_fd)
            if not _same_file(original, displaced_info) or displaced != original_data or not _same_file(displaced_info, held_info) or held_data != displaced:
                raise RuntimeError("gamelist.xml changed during operation")
            os.fsync(pygame_fd)
            exchanged = False
            final_data, final_info = _read_fd(original_fd)
            if not _same_file(held_info, final_info) or final_data != held_data:
                raise RuntimeError("gamelist.xml changed during operation")
        except BaseException:
            if exchanged:
                try:
                    _exchange_paths_at(pygame_fd, temporary, "gamelist.xml")
                    exchanged = False
                except BaseException:
                    raise
            raise
    finally:
        os.close(original_fd)


def _pygame_gamelist_snapshot(root: Path) -> tuple[bytes | None, int]:
    _mkdirs_no_symlinks(root, LEGACY_APP_PARENT_REL)
    pygame_fd = _open_shared_pygame_dir(root / LEGACY_APP_PARENT_REL)
    try:
        data, mode, _ = _gamelist_state(pygame_fd)
        if data is not None:
            gamelist_bytes(data, "preflight")
        return data, mode
    finally:
        os.close(pygame_fd)


def _restore_pygame_gamelist(root: Path, expected: tuple[bytes | None, int], previous: tuple[bytes | None, int]) -> None:
    pygame_fd = _open_shared_pygame_dir(root / LEGACY_APP_PARENT_REL)
    try:
        current, mode, info = _gamelist_state(pygame_fd)
        if current != expected[0]:
            raise RuntimeError("gamelist.xml changed during compensation")
        if previous[0] is None:
            if current is not None:
                replacement = gamelist_bytes(current, "compensation", remove=True)
                _replace_gamelist(pygame_fd, info, current, replacement, mode)
        else:
            _replace_gamelist(pygame_fd, info, current, previous[0], previous[1] if previous[1] else mode)
    finally:
        os.close(pygame_fd)


def _sync_pygame_gamelist(root: Path, remove: bool = False) -> bool:
    pygame = root / LEGACY_APP_PARENT_REL
    if remove and not pygame.exists():
        return False
    _mkdirs_no_symlinks(root, LEGACY_APP_PARENT_REL)
    pygame_fd = _open_shared_pygame_dir(pygame)
    try:
        data, mode, info = _gamelist_state(pygame_fd)
        if remove and data is None:
            return False
        replacement = gamelist_bytes(data, "registration", remove=remove)
        if replacement == data:
            return False
        _replace_gamelist(pygame_fd, info, data, replacement, mode)
        return True
    finally:
        os.close(pygame_fd)


def _required(source: Path) -> dict[str, Path]:
    paths = {
        "control-freak.pygame": source / "deploy/control-freak.pygame",
        "image.png": source / "assets/control-freak-image.png",
        "logo-proposal-1.png": source / "assets/control-freak-logo-1.png",
        "logo-proposal-2.png": source / "assets/control-freak-logo-2.png",
        "logo-proposal-3.png": source / "assets/control-freak-logo-3.png",
        "logo-proposal-4.png": source / "assets/control-freak-logo-4.png",
        "logo-proposal-5.png": source / "assets/control-freak-logo-5.png",
        "logo-proposal-6.png": source / "assets/control-freak-logo-6.png",
        "install_batocera.py": source / "deploy/install_batocera.py",
    }
    if not all(path.is_file() and not path.is_symlink() for path in paths.values()):
        raise FileNotFoundError("release source is incomplete")
    return paths


def _service_bytes(source: Path, root: Path) -> bytes:
    path = source / "deploy/control_freak"
    public_key = source / "deploy/release-public.pem"
    if path.is_symlink() or not path.is_file() or public_key.is_symlink() or not public_key.is_file():
        raise FileNotFoundError("release source is incomplete")
    text = path.read_text()
    marker = 'PUBLIC_KEY_PEM = b"__CONTROL_FREAK_RELEASE_PUBLIC_KEY__"'
    if text.count(marker) != 1:
        raise RuntimeError("Invalid service wrapper public-key marker")
    text = text.replace(marker, f"PUBLIC_KEY_PEM = {public_key.read_bytes()!r}")
    if root != Path("/userdata"):
        text = text.replace('APP = Path("/userdata/system/control-freak/app")', f"APP = Path({str((root / APP_REL).resolve())!r})")
        text = text.replace('STATE = Path("/userdata/system/control-freak")', f"STATE = Path({str((root / 'system/control-freak').resolve())!r})")
    return text.encode()


def _service_action_kwargs(root: Path) -> dict:
    kwargs = {}
    if _ACTIVE_LIFECYCLE_LOCK is not None:
        try:
            held = os.fstat(_ACTIVE_LIFECYCLE_LOCK)
            lock = os.stat(root / APP_PARENT_REL / LOCK_FILE, follow_symlinks=False)
        except OSError:
            pass
        else:
            if (held.st_dev, held.st_ino) == (lock.st_dev, lock.st_ino):
                environment = os.environ.copy()
                environment["CONTROL_FREAK_LIFECYCLE_FD"] = str(_ACTIVE_LIFECYCLE_LOCK)
                kwargs = {"env": environment, "pass_fds": (_ACTIVE_LIFECYCLE_LOCK,)}
    return kwargs


def _service_actions(root: Path, *actions: str) -> None:
    if root != Path("/userdata"):
        return
    kwargs = _service_action_kwargs(root)
    for action in actions:
        result = subprocess.run(["/usr/bin/batocera-services", action, SERVICE_NAME], check=False, **kwargs)
        if result.returncode:
            raise RuntimeError(f"Unable to {action} Control Freak service")


def _service_enabled(root: Path) -> bool:
    if root != Path("/userdata"):
        return True
    result = subprocess.run(["/usr/bin/batocera-services", "list", "user"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Unable to determine Control Freak service state")
    matches = [line.split(";", 1)[1].strip() for line in result.stdout.splitlines() if ";" in line and line.split(";", 1)[0] == SERVICE_NAME]
    if len(matches) != 1 or matches[0] not in {"*", "-"}:
        raise RuntimeError("Unable to determine Control Freak service state")
    return matches[0] == "*"


def _service_running(root: Path) -> bool:
    if root != Path("/userdata"):
        return True
    result = subprocess.run(
        ["/usr/bin/batocera-services", "status", SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
        **_service_action_kwargs(root),
    )
    if result.stdout or result.stderr:
        raise RuntimeError("Unable to determine Control Freak service state")
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError("Unable to determine Control Freak service state")


def _restore_service_state(root: Path, enabled: bool, running: bool) -> None:
    _service_actions_independent(root, "enable" if enabled else "disable", "start" if running else "stop")
    actual_enabled = _service_enabled(root)
    actual_running = _service_running(root)
    if actual_enabled != enabled or actual_running != running:
        raise RuntimeError("Unable to restore Control Freak service state")


def _prepare_owned_directory(root: Path, relative: Path, *, create: bool = True, normalize: bool = True) -> int:
    fd = _open_dir(root)
    try:
        for index, part in enumerate(relative.parts):
            final = index == len(relative.parts) - 1
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700 if final else 0o755, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            mode = info.st_mode & 0o777
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or mode & 0o022:
                os.close(child)
                raise RuntimeError("Refusing unsafe Control Freak directory")
            if final and mode != 0o700:
                if not normalize:
                    os.close(child)
                    raise RuntimeError("Refusing noncanonical Control Freak directory")
                os.fchmod(child, 0o700)
                os.fsync(child)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _prepare_state_directory(root: Path) -> None:
    root.mkdir(exist_ok=True)
    try:
        fd = _prepare_owned_directory(root, Path("system/control-freak"))
    except RuntimeError as exc:
        raise RuntimeError("Refusing unsafe Control Freak state directory") from exc
    os.close(fd)


def _runtime_dependencies(root: Path) -> None:
    if not (3, 12) <= sys.version_info[:2] < (3, 15):
        raise RuntimeError("Control Freak requires Python >=3.12,<3.15")
    if root != Path("/userdata"):
        return
    probe = "import fcntl, os, qrcode; assert hasattr(os, 'memfd_create') and hasattr(fcntl, 'F_ADD_SEALS')"
    result = subprocess.run([sys.executable, "-B", "-c", probe], check=False)
    if result.returncode:
        raise RuntimeError("Batocera Python is missing a required Control Freak runtime dependency")


def _stop_runtime_process(root: Path) -> None:
    _prepare_state_directory(root)
    pidfile = root / "system/control-freak/web.pid"
    pidfd = None
    try:
        fd = os.open(pidfile, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        with os.fdopen(fd, "r") as handle:
            pid = int(handle.read().strip())
        try:
            pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            pidfile.unlink()
            return
        expected = b"\0".join(part.encode() for part in ("/usr/bin/python3", "-I", "-B", "-c", SERVICE_CODE)) + b"\0"
        process = Path(f"/proc/{pid}")
        owned = (
            process.stat().st_uid == os.geteuid()
            and os.path.realpath(process / "exe") == os.path.realpath("/usr/bin/python3")
            and (process / "cmdline").read_bytes() == expected
        )
    except (OSError, ValueError) as exc:
        if pidfd is not None:
            os.close(pidfd)
        raise RuntimeError("Invalid Control Freak PID evidence") from exc
    if not owned or pidfd is None:
        if pidfd is not None:
            os.close(pidfd)
        raise RuntimeError("Invalid Control Freak PID evidence")
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            if not poller.poll(20_000):
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if not poller.poll(5_000):
                    raise RuntimeError("Unable to stop Control Freak service")
    finally:
        os.close(pidfd)
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass


def _runtime_lockout(root: Path, present: bool) -> bool:
    state = root / "system/control-freak"
    try:
        state_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if present:
            raise RuntimeError("Control Freak state directory is missing")
        return False
    try:
        lock_fd = os.open(RUNTIME_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                data, _ = _read_at(state_fd, RUNTIME_LOCKOUT)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
                if data != _json_bytes({"version": 1}):
                    raise RuntimeError("Invalid Control Freak runtime lockout")
            if present and not exists:
                _atomic_write_at(state_fd, RUNTIME_LOCKOUT, _json_bytes({"version": 1}), 0o600)
            elif not present and exists:
                os.unlink(RUNTIME_LOCKOUT, dir_fd=state_fd)
                os.fsync(state_fd)
            return exists
        finally:
            os.close(lock_fd)
    finally:
        os.close(state_fd)


def _fresh_activation(root: Path, present: bool | None) -> bool:
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            data, _ = _read_at(state_fd, INSTALL_ACTIVATION)
        except FileNotFoundError:
            exists = False
        else:
            exists = True
            if data != _json_bytes({"version": 1}):
                raise RuntimeError("Invalid fresh-install activation marker")
        if present is True and not exists:
            _atomic_write_at(state_fd, INSTALL_ACTIVATION, _json_bytes({"version": 1}), 0o600)
        elif present is False and exists:
            os.unlink(INSTALL_ACTIVATION, dir_fd=state_fd)
            os.fsync(state_fd)
        return exists
    finally:
        os.close(state_fd)


def _service_actions_independent(root: Path, *actions: str) -> None:
    errors = []
    for action in actions:
        try:
            _service_actions(root, action)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError("Unable to activate Control Freak service") from errors[0]


def _activate_service(root: Path, *actions: str) -> None:
    _prepare_state_directory(root)
    locked_out = _runtime_lockout(root, False)
    try:
        _service_actions_independent(root, *actions)
    except BaseException as activation_error:
        if locked_out:
            try:
                _runtime_lockout(root, True)
            except BaseException as lockout_error:
                raise RuntimeError("Unable to restore runtime lockout") from lockout_error
        raise activation_error


def _shutdown_runtime(root: Path, release_id: str, service_present: bool) -> tuple[bytes | None, bytes | None, bool, bool] | None:
    if root != Path("/userdata"):
        return
    _prepare_state_directory(root)
    service_enabled = _service_enabled(root) if service_present else False
    service_running = _service_running(root) if service_present else False
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = os.open(RUNTIME_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
    previous_audio = None
    audio_captured = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            _read_at(state_fd, RUNTIME_LOCKOUT)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("Control Freak runtime is already locked out")
        try:
            previous_web, _ = _read_at(state_fd, "web.json")
        except FileNotFoundError:
            previous_web = None
        journal = {"version": 2, "web": None if previous_web is None else previous_web.hex(), "audio": None, "audio_captured": False, "service_enabled": service_enabled, "service_running": service_running}
        _atomic_write_at(state_fd, UNINSTALL_JOURNAL, _json_bytes(journal), 0o600)
        _atomic_write_at(state_fd, "web.json", _json_bytes({"version": 1, "enabled": False, "tailscale": False, "token": ""}), 0o600)
        _atomic_write_at(state_fd, RUNTIME_LOCKOUT, _json_bytes({"version": 1}), 0o600)
        os.close(lock_fd)
        lock_fd = -1

        try:
            if service_present and service_running:
                try:
                    _service_actions(root, "stop")
                except RuntimeError:
                    if _service_running(root):
                        raise
                if _service_running(root):
                    raise RuntimeError("Unable to stop Control Freak service")
            elif not service_present:
                _stop_runtime_process(root)

            lock_fd = os.open(RUNTIME_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                previous_audio, _ = _read_at(state_fd, "audio.json")
            except FileNotFoundError:
                previous_audio = None
            audio_captured = True
            journal.update({"audio": None if previous_audio is None else previous_audio.hex(), "audio_captured": True})
            _atomic_write_at(state_fd, UNINSTALL_JOURNAL, _json_bytes(journal), 0o600)
            try:
                audio_bytes, _ = _read_at(state_fd, "audio.json")
                audio_state = json.loads(audio_bytes)
            except FileNotFoundError:
                audio_state = {"version": 1, "blocked": [], "managed": []}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Invalid Control Freak audio state") from exc
            if (
                not isinstance(audio_state, dict)
                or set(audio_state) != {"version", "blocked", "managed"}
                or type(audio_state.get("version")) is not int
                or audio_state["version"] != 1
                or not isinstance(audio_state.get("blocked"), list)
                or not isinstance(audio_state.get("managed"), list)
                or any(
                    type(value) is not str or not value or len(value) > 512 or "\0" in value or "\n" in value
                    for values in (audio_state["blocked"], audio_state["managed"])
                    for value in values
                )
            ):
                raise RuntimeError("Invalid Control Freak audio state")
            restore_failed = False
            for sink in audio_state["managed"]:
                if subprocess.run(["/usr/bin/pactl", "set-sink-mute", sink, "0"], timeout=10, check=False).returncode:
                    restore_failed = True
            if restore_failed:
                raise RuntimeError("Unable to restore Control Freak-managed audio outputs")
            _atomic_write_at(state_fd, "audio.json", _json_bytes({"version": 1, "blocked": [], "managed": []}), 0o600)
            os.close(lock_fd)
            lock_fd = -1

            if not service_present or service_enabled:
                _service_actions(root, "disable")
            if _service_enabled(root) or _service_running(root):
                raise RuntimeError("Unable to disable Control Freak service")
        except BaseException:
            if lock_fd >= 0:
                os.close(lock_fd)
            lock_fd = os.open(RUNTIME_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            for name, previous in [("web.json", previous_web), *(([("audio.json", previous_audio)] if audio_captured else []))]:
                if previous is None:
                    try:
                        os.unlink(name, dir_fd=state_fd)
                    except FileNotFoundError:
                        pass
                else:
                    _atomic_write_at(state_fd, name, previous, 0o600)
            try:
                os.unlink(RUNTIME_LOCKOUT, dir_fd=state_fd)
            except FileNotFoundError:
                pass
            os.close(lock_fd)
            lock_fd = -1
            if service_present:
                _restore_service_state(root, service_enabled, service_running)
            try:
                os.unlink(UNINSTALL_JOURNAL, dir_fd=state_fd)
            except FileNotFoundError:
                pass
            raise
        return previous_web, previous_audio, service_enabled, service_running
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(state_fd)


def _restore_runtime(root: Path, previous: tuple[bytes | None, bytes | None] | tuple[bytes | None, bytes | None, bool, bool], service_present: bool) -> None:
    service_enabled, service_running = previous[2:] if len(previous) == 4 else (service_present, service_present)
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock_fd = os.open(RUNTIME_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            for name, data in zip(("web.json", "audio.json"), previous[:2]):
                if data is None:
                    try:
                        os.unlink(name, dir_fd=state_fd)
                    except FileNotFoundError:
                        pass
                else:
                    _atomic_write_at(state_fd, name, data, 0o600)
            try:
                lockout, _ = _read_at(state_fd, RUNTIME_LOCKOUT)
            except FileNotFoundError:
                pass
            else:
                if lockout != _json_bytes({"version": 1}):
                    raise RuntimeError("Invalid Control Freak runtime lockout")
                os.unlink(RUNTIME_LOCKOUT, dir_fd=state_fd)
                os.fsync(state_fd)
        finally:
            os.close(lock_fd)
    finally:
        os.close(state_fd)
    if service_present:
        _restore_service_state(root, service_enabled, service_running)


def _read_shutdown_journal(root: Path) -> tuple[bytes | None, bytes | None] | tuple[bytes | None, bytes | None, bool, bool] | None:
    state = root / "system/control-freak"
    try:
        state_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            data, _ = _read_at(state_fd, UNINSTALL_JOURNAL)
        except FileNotFoundError:
            return None
        try:
            journal = json.loads(data)
            if (
                not isinstance(journal, dict)
                or set(journal) not in ({"version", "web", "audio", "audio_captured"}, {"version", "web", "audio", "audio_captured", "service_enabled", "service_running"})
                or type(journal.get("version")) is not int
                or journal["version"] not in {1, 2}
                or type(journal.get("audio_captured")) is not bool
                or any(value is not None and type(value) is not str for value in (journal.get("web"), journal.get("audio")))
            ):
                raise ValueError
            web = None if journal["web"] is None else bytes.fromhex(journal["web"])
            if journal["audio_captured"]:
                audio = None if journal["audio"] is None else bytes.fromhex(journal["audio"])
            else:
                try:
                    audio, _ = _read_at(state_fd, "audio.json")
                except FileNotFoundError:
                    audio = None
            if journal["version"] == 2:
                if type(journal.get("service_enabled")) is not bool or type(journal.get("service_running")) is not bool:
                    raise ValueError
                return web, audio, journal["service_enabled"], journal["service_running"]
            return web, audio
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Invalid uninstall runtime journal") from exc
    finally:
        os.close(state_fd)


def _recover_shutdown_journal(root: Path, service_present: bool) -> None:
    previous = _read_shutdown_journal(root)
    if previous is None:
        return
    _restore_runtime(root, previous, service_present)
    _clear_shutdown_journal(root)


def _clear_shutdown_journal(root: Path) -> None:
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(UNINSTALL_JOURNAL, dir_fd=state_fd)
        except FileNotFoundError:
            pass
        os.fsync(state_fd)
    finally:
        os.close(state_fd)


def _uninstall_intent(
    root: Path,
    quarantine: str | None = None,
    manifest: dict | None = None,
    identity: os.stat_result | None = None,
) -> dict | None:
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if quarantine is not None:
            if manifest is None or identity is None or not _valid_install_manifest(manifest):
                raise ValueError("uninstall intent requires a validated install")
            value = {
                "version": 2,
                "quarantine": quarantine,
                "manifest": manifest,
                "device": identity.st_dev,
                "inode": identity.st_ino,
            }
            _atomic_write_at(state_fd, UNINSTALL_INTENT, _json_bytes(value), 0o600)
            return value
        try:
            data, _ = _read_at(state_fd, UNINSTALL_INTENT)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid uninstall intent") from exc
        if (
            set(value) != {"version", "quarantine", "manifest", "device", "inode"}
            or type(value.get("version")) is not int
            or value["version"] != 2
            or type(value.get("quarantine")) is not str
            or not QUARANTINE.fullmatch(value["quarantine"])
            or not _valid_install_manifest(value.get("manifest"))
            or type(value.get("device")) is not int
            or type(value.get("inode")) is not int
        ):
            raise RuntimeError("Invalid uninstall intent")
        return value
    finally:
        os.close(state_fd)


def _clear_uninstall_intent(root: Path) -> None:
    state_fd = os.open(root / "system/control-freak", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(UNINSTALL_INTENT, dir_fd=state_fd)
        os.fsync(state_fd)
    finally:
        os.close(state_fd)


def _validate_uninstall_quarantine(target: Path, intent: dict) -> None:
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("Invalid uninstall quarantine") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != intent["device"] or info.st_ino != intent["inode"]:
        raise RuntimeError("Invalid uninstall quarantine")


def _roll_forward_uninstall(root: Path, pygame_fd: int, services_fd: int) -> None:
    intent = _uninstall_intent(root)
    if intent is None:
        return
    quarantine = intent["quarantine"]
    pygame = root / APP_PARENT_REL
    quarantines = {name for name in os.listdir(pygame) if QUARANTINE.fullmatch(name)}
    if not quarantines:
        app = root / APP_REL
        try:
            _read_at(services_fd, SERVICE_NAME)
        except FileNotFoundError:
            service_absent = True
        else:
            service_absent = False
        strategy = intent["manifest"].get("registration_strategy", "native" if intent["manifest"]["version"] == 7 else "local")
        registration_absent = all(
            not path.exists() and not path.is_symlink()
            for path in _registration_paths(root, strategy).values()
        )
        if (app.exists() or app.is_symlink()) or not service_absent or not registration_absent:
            raise RuntimeError("Ambiguous uninstall quarantine")
        if _read_shutdown_journal(root) is not None:
            _clear_shutdown_journal(root)
        _fresh_activation(root, False)
        _clear_uninstall_intent(root)
        return
    if quarantines != {quarantine}:
        raise RuntimeError("Ambiguous uninstall quarantine")
    target = pygame / quarantine
    _validate_uninstall_quarantine(target, intent)
    manifest = intent["manifest"]
    replacement = (root / APP_REL).exists()
    if not replacement:
        if manifest["version"] in {3, 4, 5, 6, 7, 8, 9}:
            try:
                service_data, service_info = _read_at(services_fd, SERVICE_NAME)
            except FileNotFoundError:
                pass
            else:
                if service_info.st_uid != os.geteuid() or service_info.st_mode & 0o777 != SERVICE_MODE or hashlib.sha256(service_data).hexdigest() != manifest["service"]["sha256"]:
                    raise RuntimeError("Modified service file")
                os.unlink(SERVICE_NAME, dir_fd=services_fd)
                os.fsync(services_fd)
        strategy = manifest.get("registration_strategy", "native" if manifest["version"] == 7 else "local")
        if strategy == "native":
            _sync_pygame_gamelist(root, remove=True)
        if manifest["version"] in {6, 7, 8, 9}:
            _remove_registration(root, manifest["registration"], allow_absent=True)
    shutil.rmtree(Path(quarantine), dir_fd=pygame_fd)
    os.fsync(pygame_fd)
    if _read_shutdown_journal(root) is not None:
        _clear_shutdown_journal(root)
    _clear_uninstall_intent(root)


def _recover_lifecycle(app: Path, services_fd: int | None) -> None:
    journal_path = app / LIFECYCLE_JOURNAL
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink():
        raise RuntimeError("Invalid lifecycle journal")
    journal = _load_json_file(journal_path, "lifecycle journal")
    if set(journal) != {"version", "before", "after"} or type(journal.get("version")) is not int or journal["version"] != 1:
        raise RuntimeError("Invalid lifecycle journal")
    before, after = journal["before"], journal["after"]
    if not _valid_install_manifest(before) or not _valid_install_manifest(after) or before["releases"] != after["releases"]:
        raise RuntimeError("Invalid lifecycle journal")
    current = _load_json_file(app / ROOT_MANIFEST, "install manifest")
    if not _valid_install_manifest(current) or current["releases"] != before["releases"]:
        raise RuntimeError("Invalid lifecycle journal")

    static = {}
    names = set(before["static"]) | set(after["static"])
    for name in names:
        target = app / name
        allowed = {manifest["static"][name] for manifest in (before, after) if name in manifest["static"]}
        mode = STATIC_MODES.get(name)
        try:
            info = target.lstat()
            if mode is None or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 not in {mode, mode | 0o020}:
                raise RuntimeError(f"Modified stable file: {name}")
            live_hash = checksum(target)
        except FileNotFoundError:
            if name in after["static"]:
                raise RuntimeError(f"Modified stable file: {name}")
            continue
        if live_hash not in allowed:
            raise RuntimeError(f"Modified stable file: {name}")
        static[name] = live_hash

    try:
        if services_fd is None:
            raise FileNotFoundError
        service_data, service_info = _read_at(services_fd, SERVICE_NAME)
        if service_info.st_mode & 0o777 != SERVICE_MODE or service_info.st_uid != os.geteuid():
            raise RuntimeError("Modified service file")
        service_hash = hashlib.sha256(service_data).hexdigest()
    except FileNotFoundError:
        service_hash = None
    allowed_services = {
        record["sha256"]
        for manifest in (before, after)
        if isinstance((record := manifest.get("service")), dict)
    }
    if service_hash is not None and service_hash not in allowed_services:
        raise RuntimeError("Modified service file")

    registration = after.get("registration")
    strategy = after.get("registration_strategy")
    if service_hash is None or set(static) != set(STATIC_FILES) or not isinstance(registration, dict) or strategy not in {"native", "local"}:
        raise RuntimeError("Invalid lifecycle journal")
    _update_registration(app.parents[2], before.get("registration", registration), registration, _installed_registration_bytes(app, strategy))
    _validate_registration(app.parents[2], registration)
    recovered = {
        "version": 9,
        "static": static,
        "service": {"name": SERVICE_NAME, "sha256": service_hash, "mode": SERVICE_MODE},
        "registration": registration,
        "registration_strategy": strategy,
        "releases": before["releases"],
    }
    app_fd = _open_dir(app)
    try:
        _atomic_write_at(app_fd, ROOT_MANIFEST, _json_bytes(recovered), 0o600)
        os.unlink(LIFECYCLE_JOURNAL, dir_fd=app_fd)
        os.fsync(app_fd)
    finally:
        os.close(app_fd)


def _update_lifecycle(app: Path, services_fd: int, stable: dict[str, bytes], service_data: bytes, registration: dict, strategy: str) -> None:
    _recover_lifecycle(app, services_fd)
    before, _ = _load_install(app, allow_missing_service=True)
    service_hash = hashlib.sha256(service_data).hexdigest()
    after = {
        "version": 9,
        "static": {name: hashlib.sha256(data).hexdigest() for name, data in stable.items()},
        "service": {"name": SERVICE_NAME, "sha256": service_hash, "mode": SERVICE_MODE},
        "registration": registration,
        "registration_strategy": strategy,
        "releases": before["releases"],
    }
    service_path = app.parents[2] / SERVICES_REL / SERVICE_NAME
    service_current = (
        service_path.is_file()
        and not service_path.is_symlink()
        and service_path.stat().st_mode & 0o777 == SERVICE_MODE
        and service_path.stat().st_uid == os.geteuid()
        and checksum(service_path) == service_hash
    )
    static_current = set(before["static"]) == set(STATIC_FILES) and all(
        (app / name).stat().st_mode & 0o777 == STATIC_MODES[name]
        and checksum(app / name) == after["static"][name]
        for name in STATIC_FILES
    )
    if before == after and service_current and static_current:
        return
    app_fd = _open_dir(app)
    try:
        _atomic_write_at(app_fd, LIFECYCLE_JOURNAL, _json_bytes({"version": 1, "before": before, "after": after}), 0o600)
        for name, data in stable.items():
            if name not in before["static"] or checksum(app / name) != after["static"][name] or (app / name).stat().st_mode & 0o777 != STATIC_MODES[name]:
                _atomic_write_at(app_fd, name, data, STATIC_MODES[name])
        for name in set(before["static"]) - set(stable):
            os.unlink(name, dir_fd=app_fd)
        os.fsync(app_fd)
        if not service_current:
            if service_path.exists():
                _atomic_write_at(services_fd, SERVICE_NAME, service_data, SERVICE_MODE)
            else:
                _atomic_create_at(services_fd, SERVICE_NAME, service_data, SERVICE_MODE)
        _update_registration(app.parents[2], before.get("registration", registration), registration, _installed_registration_bytes(app, strategy))
        _atomic_write_at(app_fd, ROOT_MANIFEST, _json_bytes(after), 0o600)
        os.unlink(LIFECYCLE_JOURNAL, dir_fd=app_fd)
        os.fsync(app_fd)
    finally:
        os.close(app_fd)


def _stable_bytes(required: dict[str, Path], app: Path) -> dict[str, bytes]:
    values = {name: path.read_bytes() for name, path in required.items()}
    if values["control-freak.pygame"] != LAUNCHER_CODE.encode():
        raise RuntimeError("Invalid Control Freak launcher")
    root = app.parents[2]
    if root != Path("/userdata"):
        values["control-freak.pygame"] = LAUNCHER_CODE.replace("/userdata", str(root)).encode()
    return values


def _migrate_legacy_install(root: Path) -> None:
    app = root / APP_REL
    legacy = root / LEGACY_APP_REL
    if app.exists() or app.is_symlink():
        if legacy.exists() or legacy.is_symlink():
            raise RuntimeError("Refusing duplicate Control Freak installations")
    elif legacy.exists() or legacy.is_symlink():
        if legacy.is_symlink() or not legacy.is_dir():
            raise RuntimeError("Refusing unknown legacy application root")
        _load_install(legacy, allow_missing_service=True)
        _prepare_state_directory(root)
        parent_fd = _open_dir(root / APP_PARENT_REL)
        legacy_fd = _open_shared_pygame_dir(root / LEGACY_APP_PARENT_REL)
        try:
            os.rename("control-freak", "app", src_dir_fd=legacy_fd, dst_dir_fd=parent_fd)
            os.fsync(legacy_fd)
            os.fsync(parent_fd)
        finally:
            os.close(legacy_fd)
            os.close(parent_fd)
    else:
        return
    gamelist = root / LEGACY_APP_PARENT_REL / "gamelist.xml"
    if gamelist.exists() and not gamelist.is_symlink():
        legacy_fd = _open_shared_pygame_dir(gamelist.parent)
        try:
            data, info = _read_at(legacy_fd, gamelist.name)
            replacement = gamelist_bytes(data, "migration", remove=True, legacy_only=True)
            if replacement != data:
                _replace_gamelist(legacy_fd, info, data, replacement, info.st_mode & 0o777)
        finally:
            os.close(legacy_fd)


def install(userdata: Path, source: Path, release_id: str, channel: str, force: bool = False, dry_run: bool = False, *, os_release_path: Path | None = None) -> list[str]:
    if not RELEASE_ID.fullmatch(release_id):
        raise ValueError("release_id must contain only letters, numbers, dot, underscore, and hyphen")
    if channel not in {"stable", "nightly"}:
        raise ValueError("channel must be stable or nightly")
    _require_supported_batocera(os_release_path)
    root = _safe_root(userdata)
    _runtime_dependencies(root)
    source = source.resolve()
    app = root / APP_REL
    operations = [f"install release {release_id}", f"set channel {channel}", "register in pygame system"]
    if dry_run:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = _snapshot_source(source, Path(temporary))
            staged, _ = _source_manifest(snapshot, release_id, Path(temporary))
            shutil.rmtree(staged)
        _launcher_boundary(root, app)
        if app.exists() or app.is_symlink():
            if (app / LIFECYCLE_JOURNAL).exists() or (app / LIFECYCLE_JOURNAL).is_symlink():
                raise RuntimeError("Interrupted lifecycle update requires recovery")
            _load_install(app, allow_missing_service=True)
        return operations

    _prepare_state_directory(root)
    services_fd = _prepare_owned_directory(root, SERVICES_REL)
    pygame_fd = _open_dir(root / APP_PARENT_REL)
    lock_fd = _lifecycle_lock(pygame_fd)
    staging: Path | None = None
    snapshot: Path | None = None
    application_staging: Path | None = None
    try:
        strategy = _registration_strategy(root, app)
        native_pygame = root / LEGACY_APP_PARENT_REL
        if strategy == "native" and (native_pygame.exists() or native_pygame.is_symlink()):
            _pygame_gamelist_snapshot(root)
        snapshot = _snapshot_source(source, root / APP_PARENT_REL)
        staging, wanted = _source_manifest(snapshot, release_id, root / APP_PARENT_REL)
        _roll_forward_uninstall(root, pygame_fd, services_fd)
        required = _required(snapshot)
        service_data = _service_bytes(snapshot, root)
        stable = _stable_bytes(required, app)
        if (app / ROOT_MANIFEST).is_file():
            installed = _load_json_file(app / ROOT_MANIFEST, "install manifest")
            if installed.get("version") in {6, 7, 8, 9} and isinstance(installed.get("registration"), dict):
                if not (app / LIFECYCLE_JOURNAL).exists() and not (app / LIFECYCLE_JOURNAL).is_symlink():
                    _validate_registration(root, installed["registration"])
            else:
                _preflight_registration(root, _registration_bytes(root, snapshot, strategy))
        else:
            _preflight_registration(root, _registration_bytes(root, snapshot, strategy))
        try:
            existing_service, info = _read_at(services_fd, SERVICE_NAME)
            if existing_service != service_data or info.st_mode & 0o777 != SERVICE_MODE or info.st_uid != os.geteuid():
                if not (app / ROOT_MANIFEST).is_file() and not (root / LEGACY_APP_REL / ROOT_MANIFEST).is_file():
                    raise RuntimeError("Refusing unknown service file")
        except FileNotFoundError:
            pass
        _migrate_legacy_install(root)
        _launcher_boundary(root, app)
        if not app.exists() and not app.is_symlink():
            for name in os.listdir(root / APP_PARENT_REL):
                if not QUARANTINE.fullmatch(name):
                    continue
                quarantine = root / APP_PARENT_REL / name
                try:
                    _load_install(quarantine, allow_missing_service=True)
                except RuntimeError:
                    continue
                raise RuntimeError("Interrupted uninstall quarantine requires recovery")
            application_staging = Path(tempfile.mkdtemp(prefix=".control-freak-stage-", dir=root / APP_PARENT_REL))
            releases = application_staging / "releases"
            releases.mkdir()
            releases_fd = _open_dir(releases)
            try:
                assert staging is not None
                os.rename(staging.name, release_id, src_dir_fd=pygame_fd, dst_dir_fd=releases_fd)
                staging = None
                os.fsync(releases_fd)
            finally:
                os.close(releases_fd)
            registration = _ensure_registration(root, snapshot, strategy)
            static = {}
            for name, data in stable.items():
                destination = application_staging / name
                destination.write_bytes(data)
                os.chmod(destination, STATIC_MODES[name])
                static[name] = checksum(destination)
            service_manifest = {"name": SERVICE_NAME, "sha256": hashlib.sha256(service_data).hexdigest(), "mode": SERVICE_MODE}
            (application_staging / ROOT_MANIFEST).write_bytes(_json_bytes({"version": 9, "static": static, "service": service_manifest, "registration": registration, "registration_strategy": strategy, "releases": [release_id]}))
            (application_staging / CURRENT).write_bytes(_json_bytes({"version": 1, "release_id": release_id, "channel": channel}))
            _fsync_tree(application_staging)
            service_missing = False
            try:
                existing_service, info = _read_at(services_fd, SERVICE_NAME)
                if existing_service != service_data or info.st_mode & 0o777 != SERVICE_MODE or info.st_uid != os.geteuid():
                    raise RuntimeError("Refusing unknown service file")
            except FileNotFoundError:
                service_missing = True
            _prepare_state_directory(root)
            _fresh_activation(root, True)
            os.rename(application_staging.name, "app", src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
            application_staging = None
            os.fsync(pygame_fd)
            if service_missing:
                try:
                    existing_service, info = _read_at(services_fd, SERVICE_NAME)
                    if existing_service != service_data or info.st_mode & 0o777 != SERVICE_MODE or info.st_uid != os.geteuid():
                        raise RuntimeError("Refusing unknown service file")
                except FileNotFoundError:
                    _atomic_create_at(services_fd, SERVICE_NAME, service_data, SERVICE_MODE)
            pygame_registered = False
            pygame_before = _pygame_gamelist_snapshot(root) if strategy == "native" else (None, 0o644)
            pygame_after = pygame_before
            locked_out = False
            try:
                if strategy == "native":
                    pygame_registered = _sync_pygame_gamelist(root)
                    pygame_after = _pygame_gamelist_snapshot(root)
                locked_out = _runtime_lockout(root, False)
                _service_actions(root, "enable", "start")
            except BaseException as activation_error:
                compensation_errors = []
                if locked_out:
                    try:
                        _runtime_lockout(root, True)
                    except BaseException as exc:
                        compensation_errors.append(exc)
                for action in ("stop", "disable"):
                    try:
                        _service_actions(root, action)
                    except BaseException as exc:
                        compensation_errors.append(exc)
                if pygame_registered:
                    try:
                        _restore_pygame_gamelist(root, pygame_after, pygame_before)
                    except BaseException as exc:
                        compensation_errors.append(exc)
                try:
                    if _service_enabled(root) or _service_running(root):
                        raise RuntimeError("service compensation did not restore disabled/stopped state")
                except BaseException as exc:
                    compensation_errors.append(exc)
                if compensation_errors:
                    raise RuntimeError("activation rollback failed; complete installation retained") from activation_error
                _fresh_activation(root, False)
                _load_install(app, stable, service_data)
                if service_missing:
                    live_service, live_info = _read_at(services_fd, SERVICE_NAME)
                    if live_service != service_data or live_info.st_mode & 0o777 != SERVICE_MODE or live_info.st_uid != os.geteuid():
                        raise RuntimeError("Service changed during install rollback")
                    os.unlink(SERVICE_NAME, dir_fd=services_fd)
                    os.fsync(services_fd)
                quarantine = f".control-freak-remove-{secrets.token_hex(12)}"
                os.rename("app", quarantine, src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
                os.fsync(pygame_fd)
                shutil.rmtree(Path(quarantine), dir_fd=pygame_fd)
                _remove_registration(root, registration)
                raise
            _fresh_activation(root, False)
            return operations

        if app.is_symlink() or not app.is_dir():
            raise RuntimeError("Refusing unknown application root")
        if (app / ROOT_MANIFEST).is_symlink() or not (app / ROOT_MANIFEST).is_file():
            raise RuntimeError("Refusing unknown application root")
        early_manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
        if not _valid_install_manifest(early_manifest):
            raise RuntimeError("Invalid install manifest")
        assert staging is not None
        tracked = early_manifest["releases"]
        published = app / "releases" / release_id
        if release_id in tracked:
            if (
                not _same_manifest(_validate_release(published, release_id), wanted)
                or (published / TRUST_STATEMENT).read_bytes() != (staging / TRUST_STATEMENT).read_bytes()
                or (published / TRUST_SIGNATURE).read_bytes() != (staging / TRUST_SIGNATURE).read_bytes()
            ):
                raise RuntimeError("Source differs from immutable published release")
        else:
            unknown = set(os.listdir(app / "releases")) - set(tracked)
            if unknown:
                if unknown != {release_id}:
                    raise RuntimeError(f"Unknown release: {sorted(unknown)[0]}")
                if (
                    not _same_manifest(_validate_release(published, release_id), wanted)
                    or (published / TRUST_STATEMENT).read_bytes() != (staging / TRUST_STATEMENT).read_bytes()
                    or (published / TRUST_SIGNATURE).read_bytes() != (staging / TRUST_SIGNATURE).read_bytes()
                ):
                    raise RuntimeError("Published release differs from source")
                tracked.append(release_id)
                app_fd = _open_dir(app)
                try:
                    _atomic_write_at(app_fd, ROOT_MANIFEST, _json_bytes(early_manifest), 0o600)
                finally:
                    os.close(app_fd)
        _recover_activation(root, app)
        _recover_lifecycle(app, services_fd)
        registration = _ensure_registration(root, snapshot, strategy)
        manifest, current = _load_install(app, allow_missing_service=True)
        assert staging is not None
        _prepare_state_directory(root)
        if release_id not in manifest["releases"]:
            releases_fd = _open_dir(app / "releases")
            try:
                os.rename(staging.name, release_id, src_dir_fd=pygame_fd, dst_dir_fd=releases_fd)
                staging = None
                os.fsync(releases_fd)
            finally:
                os.close(releases_fd)
            manifest["releases"].append(release_id)
            app_fd = _open_dir(app)
            try:
                _atomic_write_at(app_fd, ROOT_MANIFEST, _json_bytes(manifest), 0o600)
            finally:
                os.close(app_fd)
        if strategy == "native":
            _sync_pygame_gamelist(root)
        _update_lifecycle(app, services_fd, stable, service_data, registration, strategy)
        manifest, current = _load_install(app, stable, service_data)
        _recover_shutdown_journal(root, True)
        fresh_activation = _fresh_activation(root, None)

        releases = manifest["releases"]
        if release_id in releases:
            if current["release_id"] == release_id and current["channel"] == channel and not force:
                if fresh_activation:
                    try:
                        _activate_service(root, "enable", "start")
                    except BaseException as activation_error:
                        try:
                            _restore_service_state(root, False, False)
                        except BaseException as compensation_error:
                            raise RuntimeError("activation rollback failed; complete installation retained") from compensation_error
                        raise activation_error
                    _fresh_activation(root, False)
                elif _service_enabled(root):
                    _activate_service(root, "stop", "start")
                return [f"already installed {release_id}"]

        after = {"version": 1, "release_id": release_id, "channel": channel}
        enabled = _service_enabled(root)
        running = _service_running(root)
        app_fd = _open_dir(app)
        try:
            _atomic_write_at(
                app_fd,
                ACTIVATION_JOURNAL,
                _json_bytes({
                    "version": 2,
                    "before": current,
                    "after": after,
                    "service_enabled": enabled,
                    "service_running": running,
                }),
                0o600,
            )
        finally:
            os.close(app_fd)
        if force and current["release_id"] == release_id:
            operations[0] = f"reselect release {release_id}"
        try:
            if running:
                _service_actions(root, "stop")
            app_fd = _open_dir(app)
            try:
                _atomic_write_at(app_fd, CURRENT, _json_bytes(after), 0o600)
            finally:
                os.close(app_fd)
            if running:
                _service_actions(root, "start")
            if _service_enabled(root) != enabled or _service_running(root) != running:
                raise RuntimeError("Service state changed during update")
        except BaseException:
            if running:
                try:
                    _service_actions(root, "stop")
                except BaseException:
                    raise RuntimeError("update rollback failed; activation journal retained")
            app_fd = _open_dir(app)
            try:
                _atomic_write_at(app_fd, CURRENT, _json_bytes(current), 0o600)
            finally:
                os.close(app_fd)
            if running:
                try:
                    _service_actions(root, "start")
                except BaseException:
                    raise RuntimeError("update rollback failed; activation journal retained")
            app_fd = _open_dir(app)
            try:
                os.unlink(ACTIVATION_JOURNAL, dir_fd=app_fd)
                os.fsync(app_fd)
            finally:
                os.close(app_fd)
            raise
        app_fd = _open_dir(app)
        try:
            os.unlink(ACTIVATION_JOURNAL, dir_fd=app_fd)
            os.fsync(app_fd)
        finally:
            os.close(app_fd)
        return operations
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if application_staging is not None:
            shutil.rmtree(application_staging, ignore_errors=True)
        if snapshot is not None:
            shutil.rmtree(snapshot, ignore_errors=True)
        os.close(lock_fd)
        os.close(pygame_fd)
        os.close(services_fd)


def rollback_info(userdata: Path) -> str | None:
    root = _safe_root(userdata)
    app = root / APP_REL
    if (app / ACTIVATION_JOURNAL).exists() or (app / LIFECYCLE_JOURNAL).exists():
        raise RuntimeError("Interrupted activation requires recovery")
    _registration_strategy(root, app)
    manifest, current = _load_install(app)
    predecessors = [release for release in manifest["releases"] if release != current["release_id"]]
    return predecessors[-1] if predecessors else None


def rollback(userdata: Path) -> list[str]:
    root = _safe_root(userdata)
    app = root / APP_REL
    pygame_fd = _open_dir(root / APP_PARENT_REL)
    lock_fd = _lifecycle_lock(pygame_fd)
    try:
        _registration_strategy(root, app)
        _recover_activation(root, app)
        manifest, current = _load_install(app)
        predecessors = [release for release in manifest["releases"] if release != current["release_id"]]
        if not predecessors:
            raise RuntimeError("No verified predecessor is available")
        target = predecessors[-1]
        after = {"version": 1, "release_id": target, "channel": current["channel"]}
        enabled = _service_enabled(root)
        running = _service_running(root)
        app_fd = _open_dir(app)
        try:
            _atomic_write_at(app_fd, ACTIVATION_JOURNAL, _json_bytes({
                "version": 2,
                "before": current,
                "after": after,
                "service_enabled": enabled,
                "service_running": running,
            }), 0o600)
        finally:
            os.close(app_fd)
        try:
            if running:
                _service_actions(root, "stop")
            app_fd = _open_dir(app)
            try:
                _atomic_write_at(app_fd, CURRENT, _json_bytes(after), 0o600)
            finally:
                os.close(app_fd)
            if running:
                _service_actions(root, "start")
        except BaseException as activation_error:
            compensation_errors = []
            if running:
                try:
                    _service_actions(root, "stop")
                except BaseException as exc:
                    compensation_errors.append(exc)
            try:
                app_fd = _open_dir(app)
                try:
                    _atomic_write_at(app_fd, CURRENT, _json_bytes(current), 0o600)
                finally:
                    os.close(app_fd)
                if running:
                    _service_actions(root, "start")
            except BaseException as exc:
                compensation_errors.append(exc)
            if compensation_errors:
                raise RuntimeError("rollback compensation failed; activation journal retained") from activation_error
            app_fd = _open_dir(app)
            try:
                os.unlink(ACTIVATION_JOURNAL, dir_fd=app_fd)
                os.fsync(app_fd)
            finally:
                os.close(app_fd)
            raise
        if _service_enabled(root) != enabled or _service_running(root) != running:
            raise RuntimeError("Service state changed during rollback; activation journal retained")
        app_fd = _open_dir(app)
        try:
            os.unlink(ACTIVATION_JOURNAL, dir_fd=app_fd)
            os.fsync(app_fd)
        finally:
            os.close(app_fd)
        return [f"activate release {target}"]
    finally:
        os.close(lock_fd)
        os.close(pygame_fd)


def uninstall(userdata: Path, dry_run: bool = False) -> list[str]:
    root = _safe_root(userdata)
    app = root / APP_REL
    pygame = root / APP_PARENT_REL
    if not pygame.exists() and not pygame.is_symlink():
        return []
    if pygame.is_symlink() or not pygame.is_dir():
        raise RuntimeError(f"Refusing unsafe install parent: {pygame}")
    pygame_fd = _open_owned_dir(pygame)
    services_fd = None
    try:
        services_fd = _prepare_owned_directory(root, SERVICES_REL, create=False, normalize=not dry_run)
    except FileNotFoundError:
        pass
    except BaseException:
        os.close(pygame_fd)
        raise
    lock_fd = _lifecycle_lock(pygame_fd)
    try:
        uninstall_intent = _uninstall_intent(root)
        preflight_manifest = None
        if (app / ROOT_MANIFEST).is_file() and not (app / ROOT_MANIFEST).is_symlink():
            preflight_manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
        elif uninstall_intent is not None:
            preflight_manifest = uninstall_intent["manifest"]
        local_registration = isinstance(preflight_manifest, dict) and _valid_install_manifest(preflight_manifest) and preflight_manifest.get("registration_strategy") == "local"
        native_pygame = root / LEGACY_APP_PARENT_REL
        if not local_registration and (native_pygame.exists() or native_pygame.is_symlink()):
            if not _native_registration_supported(root, probe_exchange=False):
                raise RuntimeError("Native registration requires an owned non-writable pygame directory")
            _pygame_gamelist_snapshot(root)
        planned_quarantine = None if uninstall_intent is None else uninstall_intent["quarantine"]
        if app.exists() or app.is_symlink():
            if app.is_symlink() or not app.is_dir():
                raise RuntimeError("Refusing unknown application root")
            target = app
            if uninstall_intent is not None:
                intent_info = target.stat()
                intent_manifest, _ = _load_install(target, allow_missing_service=True)
                if (
                    intent_info.st_dev != uninstall_intent["device"]
                    or intent_info.st_ino != uninstall_intent["inode"]
                    or intent_manifest != uninstall_intent["manifest"]
                ):
                    raise RuntimeError("Ambiguous uninstall application")
        else:
            quarantines = [name for name in os.listdir(pygame) if QUARANTINE.fullmatch(name)]
            if not quarantines:
                if planned_quarantine is None:
                    return []
                if services_fd is None:
                    service_absent = True
                else:
                    try:
                        _read_at(services_fd, SERVICE_NAME)
                    except FileNotFoundError:
                        service_absent = True
                    else:
                        service_absent = False
                registration_absent = all(
                    not path.exists() and not path.is_symlink()
                    for path in _registration_paths(root, "local" if local_registration else "native").values()
                )
                if not service_absent or not registration_absent:
                    raise RuntimeError("Ambiguous uninstall quarantine")
                if dry_run:
                    return ["complete interrupted uninstall"]
                if _read_shutdown_journal(root) is not None:
                    _clear_shutdown_journal(root)
                _fresh_activation(root, False)
                _clear_uninstall_intent(root)
                return []
            if planned_quarantine is None or set(quarantines) != {planned_quarantine}:
                raise RuntimeError("Ambiguous uninstall quarantine")
            target = pygame / planned_quarantine
            assert uninstall_intent is not None
            _validate_uninstall_quarantine(target, uninstall_intent)
        if target == app and ((target / LIFECYCLE_JOURNAL).exists() or (target / LIFECYCLE_JOURNAL).is_symlink()):
            if dry_run:
                raise RuntimeError("Interrupted lifecycle update requires recovery")
            _recover_lifecycle(target, services_fd)
        if target == app:
            manifest, current = _load_install(target, allow_missing_service=True)
        else:
            assert uninstall_intent is not None
            manifest, current = uninstall_intent["manifest"], None
        if target != app:
            _read_shutdown_journal(root)
        operations = [f"remove {APP_REL}", "unregister from pygame system"]
        if dry_run:
            return operations
        # Recheck the complete owned tree before the first mutation.
        if target == app:
            _load_install(target, allow_missing_service=True)
        else:
            assert uninstall_intent is not None
            _validate_uninstall_quarantine(target, uninstall_intent)
        service_info = None
        service_data = b""
        if manifest["version"] in {3, 4, 5, 6, 7, 8, 9}:
            try:
                if services_fd is None:
                    raise FileNotFoundError
                service_data, service_info = _read_at(services_fd, SERVICE_NAME)
            except FileNotFoundError:
                service_data = b""
            if service_info is not None and (
                service_info.st_mode & 0o777 != SERVICE_MODE
                or service_info.st_uid != os.geteuid()
                or hashlib.sha256(service_data).hexdigest() != manifest["service"]["sha256"]
            ):
                raise RuntimeError("Modified service file")

        if target == app:
            pending_shutdown = _read_shutdown_journal(root)
            if pending_shutdown is not None:
                _recover_shutdown_journal(root, service_info is not None)
        if service_info is not None:
            assert services_fd is not None
            current_service, current_info = _read_at(services_fd, SERVICE_NAME)
            if not _same_file(service_info, current_info) or hashlib.sha256(current_service).hexdigest() != manifest["service"]["sha256"]:
                raise RuntimeError("Service changed during operation")
        if service_info is not None:
            assert services_fd is not None
            current_service, current_info = _read_at(services_fd, SERVICE_NAME)
            if not _same_file(service_info, current_info) or hashlib.sha256(current_service).hexdigest() != manifest["service"]["sha256"]:
                raise RuntimeError("Service changed during operation")
        if planned_quarantine is None:
            planned_quarantine = f".control-freak-remove-{secrets.token_hex(12)}"
            uninstall_intent = _uninstall_intent(root, planned_quarantine, manifest, target.stat())
        runtime_snapshot = None
        if target == app and manifest["version"] in {3, 4, 5, 6, 7, 8, 9}:
            assert current is not None
            try:
                runtime_snapshot = _shutdown_runtime(root, current["release_id"], service_info is not None)
            except BaseException:
                _clear_uninstall_intent(root)
                raise
        quarantine = None
        app_quarantined = False
        service_removed = False
        pygame_unregistered = False

        try:
            if service_info is not None:
                assert services_fd is not None
                current_service, current_info = _read_at(services_fd, SERVICE_NAME)
                if not _same_file(service_info, current_info) or hashlib.sha256(current_service).hexdigest() != manifest["service"]["sha256"]:
                    raise RuntimeError("Service changed during operation")
            if target == app:
                quarantine = planned_quarantine
                os.rename("app", quarantine, src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
                app_quarantined = True
                os.fsync(pygame_fd)
                target = pygame / quarantine
            if service_info is not None:
                assert services_fd is not None
                os.unlink(SERVICE_NAME, dir_fd=services_fd)
                service_removed = True
                os.fsync(services_fd)
            strategy = manifest.get("registration_strategy", "native" if manifest["version"] == 7 else "local")
            if strategy == "native":
                pygame_unregistered = _sync_pygame_gamelist(root, remove=True)
        except BaseException:
            if service_removed:
                assert services_fd is not None
                _atomic_create_at(services_fd, SERVICE_NAME, service_data, SERVICE_MODE)

            if app_quarantined:
                assert quarantine is not None
                os.rename(quarantine, "app", src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
                os.fsync(pygame_fd)
            if pygame_unregistered:
                _sync_pygame_gamelist(root)
            if runtime_snapshot is not None:
                _restore_runtime(root, runtime_snapshot, service_info is not None)
            _clear_uninstall_intent(root)
            raise
        if runtime_snapshot is not None:
            _clear_shutdown_journal(root)
        if manifest["version"] in {6, 7, 8, 9}:
            _remove_registration(root, manifest["registration"], allow_absent=True)
        shutil.rmtree(Path(target.name), dir_fd=pygame_fd, ignore_errors=True)
        if target.exists():
            raise RuntimeError("Unable to remove Control Freak quarantine")
        os.fsync(pygame_fd)
        if _read_shutdown_journal(root) is not None:
            _clear_shutdown_journal(root)
        _fresh_activation(root, False)
        _clear_uninstall_intent(root)
        return []
    finally:
        os.close(lock_fd)
        if services_fd is not None:
            os.close(services_fd)
        os.close(pygame_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or update Control Freak on Batocera")
    parser.add_argument("--root", type=Path, default=Path("/userdata"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release-id")
    parser.add_argument("--channel", choices=("stable", "nightly"), default="stable")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--rollback-info", action="store_true")
    args = parser.parse_args()
    if sum((args.uninstall, args.rollback, args.rollback_info)) > 1:
        parser.error("choose only one lifecycle action")
    if args.rollback_info:
        previous = rollback_info(args.root)
        operations = [] if previous is None else [previous]
    elif args.rollback:
        operations = rollback(args.root)
    elif args.uninstall:
        operations = uninstall(args.root, args.dry_run)
    else:
        if not args.release_id:
            parser.error("--release-id is required for install")
        operations = install(args.root, args.source, args.release_id, args.channel, args.force, args.dry_run)
    print("\n".join(operations))


if __name__ == "__main__":
    main()
