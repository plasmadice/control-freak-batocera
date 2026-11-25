#!/usr/bin/env python3
"""Crash-safe Control Freak installer for Batocera userdata."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
from xml.parsers import expat
from pathlib import Path, PurePosixPath

APP_REL = Path("roms/pygame/control-freak")
PYGAME_REL = Path("roms/pygame")
GAME_PATH = "./control-freak/control-freak.pygame"
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATIC_FILES = (
    "control-freak.pygame",
    "image.png",
    "logo-proposal-1.png",
    "logo-proposal-2.png",
    "logo-proposal-3.png",
    "install_batocera.py",
)
STATIC_MODES = {
    "control-freak.pygame": 0o755,
    "image.png": 0o644,
    "logo-proposal-1.png": 0o644,
    "logo-proposal-2.png": 0o644,
    "logo-proposal-3.png": 0o644,
    "install_batocera.py": 0o755,
}
ROOT_MANIFEST = "install-manifest.json"
CURRENT = "current.json"
RELEASE_MANIFEST = "manifest.json"
QUARANTINE = re.compile(r"^\.control-freak-remove-[0-9a-f]{24}$")
LOCK_FILE = ".control-freak.lock"
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


def _mkdirs_no_symlinks(root: Path, relative: Path) -> None:
    cursor = root
    cursor.mkdir(exist_ok=True)
    for part in relative.parts:
        cursor /= part
        try:
            cursor.mkdir()
        except FileExistsError:
            if cursor.is_symlink() or not cursor.is_dir():
                raise RuntimeError(f"Refusing unsafe install parent: {cursor}")


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _lifecycle_lock(directory_fd: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    lock_fd = os.open(LOCK_FILE, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Invalid lifecycle lock")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _read_at(directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Expected regular file: {name}")
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(), info
    finally:
        os.close(fd)


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
            relative = path.relative_to(destination).as_posix()
            files.append({"path": relative, "sha256": checksum(path), "mode": path.stat().st_mode & 0o777})
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
    expected = {RELEASE_MANIFEST}
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


def _same_manifest(left: dict, right: dict) -> bool:
    return left == right


def _load_install(app: Path, expected_static: dict[str, bytes] | None = None) -> tuple[dict, dict]:
    manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
    static = manifest.get("static")
    releases = manifest.get("releases")
    if (
        set(manifest) != {"version", "static", "releases"}
        or type(manifest.get("version")) is not int
        or manifest["version"] != 2
        or not isinstance(static, dict)
        or set(static) != set(STATIC_FILES)
        or any(type(value) is not str or not SHA256.fullmatch(value) for value in static.values())
        or not isinstance(releases, list)
        or any(type(item) is not str or not RELEASE_ID.fullmatch(item) for item in releases)
        or len(releases) != len(set(releases))
    ):
        raise RuntimeError("Invalid install manifest")
    for name in STATIC_FILES:
        target = app / name
        try:
            matches = (
                not target.is_symlink()
                and target.is_file()
                and checksum(target) == manifest["static"][name]
                and (target.stat().st_mode & 0o777) in {STATIC_MODES[name], STATIC_MODES[name] | 0o020}
            )
        except OSError:
            matches = False
        if not matches:
            raise RuntimeError(f"Modified stable file: {name}")
        if expected_static is not None and manifest["static"][name] != hashlib.sha256(expected_static[name]).hexdigest():
            raise RuntimeError(f"Lifecycle protocol mismatch: {name}")
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
    expected = set(STATIC_FILES) | {ROOT_MANIFEST, CURRENT, "releases"}
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


def gamelist_bytes(data: bytes | None, release_id: str, remove: bool = False) -> bytes:
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
    matches = [game for game in root.findall("game") if game.findtext("path") == GAME_PATH]
    if len(matches) > 1 or matches and matches[0].findtext("name") != "Control Freak":
        raise RuntimeError("Refusing to replace an unknown gamelist entry")
    if remove and not matches:
        assert data is not None
        return data
    if matches:
        root.remove(matches[0])
    if not remove:
        game = ET.SubElement(root, "game")
        for key, value in {
            "path": GAME_PATH,
            "name": "Control Freak",
            "desc": "Controller-first settings and management for Batocera.",
            "genre": "Utility",
            "image": "./control-freak/image.png",
            "developer": "Haku",
            "publisher": "Haku",
            "players": "1",
        }.items():
            ET.SubElement(game, key).text = value
    ET.indent(root, space="\t")
    return prefix + ET.tostring(root, encoding="utf-8") + suffix


def _gamelist_state(pygame_fd: int) -> tuple[bytes | None, int, os.stat_result | None]:
    try:
        data, info = _read_at(pygame_fd, "gamelist.xml")
        return data, info.st_mode & 0o777, info
    except FileNotFoundError:
        return None, 0o644, None


def _same_file(left: os.stat_result | None, right: os.stat_result | None) -> bool:
    if left is None or right is None:
        return left is right
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)


def _replace_gamelist(pygame_fd: int, original: os.stat_result | None, data: bytes, mode: int) -> None:
    try:
        current = os.stat("gamelist.xml", dir_fd=pygame_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if not _same_file(original, current):
        raise RuntimeError("gamelist.xml changed during operation")
    _atomic_write_at(pygame_fd, "gamelist.xml", data, mode)


def _required(source: Path) -> dict[str, Path]:
    paths = {
        "control-freak.pygame": source / "deploy/control-freak.pygame",
        "image.png": source / "assets/control-freak-image.png",
        "logo-proposal-1.png": source / "assets/control-freak-logo-1.png",
        "logo-proposal-2.png": source / "assets/control-freak-logo-2.png",
        "logo-proposal-3.png": source / "assets/control-freak-logo-3.png",
        "install_batocera.py": source / "deploy/install_batocera.py",
    }
    if not all(path.is_file() and not path.is_symlink() for path in paths.values()):
        raise FileNotFoundError("release source is incomplete")
    return paths


def _stable_bytes(required: dict[str, Path], app: Path) -> dict[str, bytes]:
    artifacts = {name: path.read_bytes() for name, path in required.items()}
    try:
        launcher = artifacts["control-freak.pygame"].decode()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Lifecycle protocol mismatch: control-freak.pygame") from exc
    default = 'APP_ROOT = "/userdata/roms/pygame/control-freak"'
    if launcher.count(default) != 1:
        raise RuntimeError("Lifecycle protocol mismatch: control-freak.pygame")
    artifacts["control-freak.pygame"] = launcher.replace(default, f"APP_ROOT = {str(app)!r}").encode()
    return artifacts


def install(userdata: Path, source: Path, release_id: str, channel: str, force: bool = False, dry_run: bool = False) -> list[str]:
    if not RELEASE_ID.fullmatch(release_id):
        raise ValueError("release_id must contain only letters, numbers, dot, underscore, and hyphen")
    if channel not in {"stable", "nightly"}:
        raise ValueError("channel must be stable or nightly")
    root = _safe_root(userdata)
    source = source.resolve()
    required = _required(source)
    app = root / APP_REL
    stable = _stable_bytes(required, app)
    operations = [f"install release {release_id}", f"set channel {channel}", f"register {GAME_PATH}"]
    if dry_run:
        if app.exists() or app.is_symlink():
            _load_install(app, stable)
        return operations

    _mkdirs_no_symlinks(root, PYGAME_REL)
    pygame_fd = _open_dir(root / PYGAME_REL)
    lock_fd = _lifecycle_lock(pygame_fd)
    staging: Path | None = None
    gamelist_temporary: str | None = None
    try:
        gamelist, gamelist_mode, gamelist_info = _gamelist_state(pygame_fd)
        new_gamelist = gamelist_bytes(gamelist, release_id)
        if not app.exists() and not app.is_symlink():
            gamelist_temporary = _stage_write_at(pygame_fd, "gamelist.xml", new_gamelist, gamelist_mode)
            staging = Path(tempfile.mkdtemp(prefix=".control-freak-stage-", dir=root / PYGAME_REL))
            releases = staging / "releases"
            releases.mkdir()
            release = releases / release_id
            release.mkdir()
            _release_source(source, release, release_id)
            static = {}
            for name, data in stable.items():
                destination = staging / name
                destination.write_bytes(data)
                os.chmod(destination, STATIC_MODES[name])
                static[name] = checksum(destination)
            (staging / ROOT_MANIFEST).write_bytes(_json_bytes({"version": 2, "static": static, "releases": [release_id]}))
            (staging / CURRENT).write_bytes(_json_bytes({"version": 1, "release_id": release_id, "channel": channel}))
            _fsync_tree(staging)
            os.rename(staging.name, "control-freak", src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
            staging = None
            os.fsync(pygame_fd)
            try:
                current_gamelist = os.stat("gamelist.xml", dir_fd=pygame_fd, follow_symlinks=False)
            except FileNotFoundError:
                current_gamelist = None
            if not _same_file(gamelist_info, current_gamelist):
                raise RuntimeError("gamelist.xml changed during operation")
            _commit_write_at(pygame_fd, gamelist_temporary, "gamelist.xml")
            gamelist_temporary = None
            return operations

        if app.is_symlink() or not app.is_dir():
            raise RuntimeError("Refusing unknown application root")
        if (app / ROOT_MANIFEST).is_symlink() or not (app / ROOT_MANIFEST).is_file():
            raise RuntimeError("Refusing unknown application root")
        try:
            manifest, current = _load_install(app, stable)
        except RuntimeError as exc:
            # A fully published target release may be the only untracked object after a crash.
            if str(exc) != f"Unknown release: {release_id}":
                raise
            manifest = _load_json_file(app / ROOT_MANIFEST, "install manifest")
            tracked = manifest.get("releases") if isinstance(manifest.get("releases"), list) else []
            unknown = set(os.listdir(app / "releases")) - set(tracked)
            if unknown != {release_id}:
                raise exc
            staged_source, wanted = _source_manifest(source, release_id, root / PYGAME_REL)
            try:
                if not _same_manifest(_validate_release(app / "releases" / release_id, release_id), wanted):
                    raise RuntimeError("Published release differs from source")
            finally:
                shutil.rmtree(staged_source, ignore_errors=True)
            manifest["releases"].append(release_id)
            app_fd = _open_dir(app)
            try:
                _atomic_write_at(app_fd, ROOT_MANIFEST, _json_bytes(manifest), 0o600)
            finally:
                os.close(app_fd)
            manifest, current = _load_install(app, stable)

        releases = manifest["releases"]
        if release_id in releases:
            staged_source, wanted = _source_manifest(source, release_id, root / PYGAME_REL)
            try:
                if not _same_manifest(_validate_release(app / "releases" / release_id, release_id), wanted):
                    raise RuntimeError("Source differs from immutable published release")
            finally:
                shutil.rmtree(staged_source, ignore_errors=True)
            if current["release_id"] == release_id and current["channel"] == channel and not force:
                if new_gamelist != gamelist:
                    _replace_gamelist(pygame_fd, gamelist_info, new_gamelist, gamelist_mode)
                    return [f"repair registration for {release_id}"]
                return [f"already installed {release_id}"]
        else:
            staging, _ = _source_manifest(source, release_id, root / PYGAME_REL)
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

        # Registration metadata is release-independent, so a valid entry is not republished.
        if new_gamelist != gamelist:
            _replace_gamelist(pygame_fd, gamelist_info, new_gamelist, gamelist_mode)
        app_fd = _open_dir(app)
        try:
            _atomic_write_at(app_fd, CURRENT, _json_bytes({"version": 1, "release_id": release_id, "channel": channel}), 0o600)
        finally:
            os.close(app_fd)
        if force and current["release_id"] == release_id:
            operations[0] = f"reselect release {release_id}"
        return operations
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if gamelist_temporary is not None:
            try:
                os.unlink(gamelist_temporary, dir_fd=pygame_fd)
            except (FileNotFoundError, OSError):
                pass
        os.close(lock_fd)
        os.close(pygame_fd)


def uninstall(userdata: Path, dry_run: bool = False) -> list[str]:
    root = _safe_root(userdata)
    app = root / APP_REL
    pygame = root / PYGAME_REL
    if not pygame.exists() and not pygame.is_symlink():
        return []
    if pygame.is_symlink() or not pygame.is_dir():
        raise RuntimeError(f"Refusing unsafe install parent: {pygame}")
    pygame_fd = _open_dir(pygame)
    lock_fd = _lifecycle_lock(pygame_fd)
    try:
        if app.exists() or app.is_symlink():
            if app.is_symlink() or not app.is_dir():
                raise RuntimeError("Refusing unknown application root")
            target = app
        else:
            quarantines = [name for name in os.listdir(pygame) if QUARANTINE.fullmatch(name)]
            if not quarantines:
                return []
            if len(quarantines) > 1:
                raise RuntimeError("Multiple uninstall quarantines")
            target = pygame / quarantines[0]
            if target.is_symlink() or not target.is_dir():
                raise RuntimeError("Invalid uninstall quarantine")
        manifest, current = _load_install(target)
        gamelist, mode, info = _gamelist_state(pygame_fd)
        if gamelist is None:
            raise RuntimeError("Managed gamelist is missing")
        replacement = gamelist_bytes(gamelist, current["release_id"], remove=True)
        operations = [f"remove {APP_REL}", f"unregister {GAME_PATH}"]
        if dry_run:
            return operations
        # Recheck the complete owned tree before the first mutation.
        _load_install(target)
        if replacement != gamelist:
            _replace_gamelist(pygame_fd, info, replacement, mode)
        if target == app:
            quarantine = f".control-freak-remove-{secrets.token_hex(12)}"
            os.rename("control-freak", quarantine, src_dir_fd=pygame_fd, dst_dir_fd=pygame_fd)
            os.fsync(pygame_fd)
            target = pygame / quarantine
        shutil.rmtree(Path(target.name), dir_fd=pygame_fd)
        return []
    finally:
        os.close(lock_fd)
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
    args = parser.parse_args()
    if args.uninstall:
        operations = uninstall(args.root, args.dry_run)
    else:
        if not args.release_id:
            parser.error("--release-id is required for install")
        operations = install(args.root, args.source, args.release_id, args.channel, args.force, args.dry_run)
    print("\n".join(operations))


if __name__ == "__main__":
    main()
