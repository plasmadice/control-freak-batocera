from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

LOCK_NAME = "web.lock"
LOCKOUT_NAME = "runtime.lockout"


def _open_directory(path: Path, *, create: bool) -> int:
    absolute = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
            raise RuntimeError("Refusing unsafe Control Freak state directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def locked(path: Path, *, lock_name: str = LOCK_NAME, yield_lock: bool = False):
    directory_fd = _open_directory(path.parent, create=True)
    lock_fd = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise RuntimeError("Invalid Control Freak lock")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            lockout_fd = os.open(LOCKOUT_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        else:
            os.close(lockout_fd)
            raise RuntimeError("Control Freak is uninstalled")
        yield (directory_fd, path.name, lock_fd) if yield_lock else (directory_fd, path.name)
    finally:
        os.close(lock_fd)
        os.close(directory_fd)


@contextmanager
def opened(path: Path):
    directory_fd = _open_directory(path.parent, create=False)
    try:
        try:
            lockout_fd = os.open(LOCKOUT_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        else:
            os.close(lockout_fd)
            raise RuntimeError("Control Freak is uninstalled")
        yield directory_fd, path.name
    finally:
        os.close(directory_fd)


def read_json(directory_fd: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("Invalid state file")
        with os.fdopen(fd, "r", closefd=False) as handle:
            return json.load(handle)
    finally:
        os.close(fd)


def write_json(directory_fd: int, name: str, value: dict) -> None:
    data = (json.dumps(value, sort_keys=True) + "\n").encode()
    temporary = f".{name}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
