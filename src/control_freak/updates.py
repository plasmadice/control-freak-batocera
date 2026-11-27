from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

API_URL = "https://api.github.com/repos/plasmadice/control-freak-batocera/releases?per_page=20"
REPOSITORY = "plasmadice/control-freak-batocera"
METADATA_NAME = "control-freak-release.json"
SIGNATURE_NAME = "control-freak-release.sig"
ARTIFACT_NAME = "control-freak-release.zip"
SEQUENCE_STATE = "update-sequences.json"
MAX_API_BYTES = 512 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class UpdateError(RuntimeError):
    pass


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _github_url(url: object, *, api: bool = False) -> str:
    if not isinstance(url, str):
        raise UpdateError("Invalid GitHub asset URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise UpdateError("Invalid GitHub asset URL")
    if api:
        if parsed.netloc != "api.github.com" or parsed.path != "/repos/plasmadice/control-freak-batocera/releases":
            raise UpdateError("Invalid GitHub API URL")
    elif parsed.netloc != "github.com" or not parsed.path.startswith("/plasmadice/control-freak-batocera/releases/download/"):
        raise UpdateError("Invalid GitHub asset URL")
    return url


def _redirect_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or host not in {
        "github.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"
    } and not host.endswith(".githubusercontent.com"):
        raise UpdateError("Unsafe GitHub redirect")


class _GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _redirect_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request, timeout: int):
    return build_opener(_GitHubRedirects()).open(request, timeout=timeout)


def _read_response(response, limit: int, *, api: bool = False) -> bytes:
    final = response.geturl() if hasattr(response, "geturl") else getattr(response, "url", "")
    if api:
        if final != API_URL:
            raise UpdateError("Invalid GitHub API URL")
    else:
        _redirect_url(final)
    if getattr(response, "status", 200) != 200:
        raise UpdateError("GitHub request failed")
    length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if length is not None:
        try:
            if int(length) < 0 or int(length) > limit:
                raise UpdateError("GitHub response is too large")
        except ValueError as exc:
            raise UpdateError("Invalid GitHub response length") from exc
    data = bytearray()
    while True:
        chunk = response.read(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise UpdateError("GitHub response is too large")
    return bytes(data)


def _request(url: str, limit: int, opener=_open) -> bytes:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "control-freak-batocera"})
    try:
        with opener(request, timeout=10) as response:
            return _read_response(response, limit, api=url == API_URL)
    except UpdateError:
        raise
    except (OSError, TimeoutError) as exc:
        raise UpdateError("GitHub request failed") from exc


def _assets(value: object, tag: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise UpdateError("Invalid GitHub release assets")
    wanted = {METADATA_NAME, SIGNATURE_NAME, ARTIFACT_NAME}
    result: dict[str, dict] = {}
    prefix = f"/plasmadice/control-freak-batocera/releases/download/{quote(tag, safe='')}/"
    for item in value:
        if not isinstance(item, dict):
            raise UpdateError("Invalid GitHub release asset")
        name, size, url = item.get("name"), item.get("size"), item.get("browser_download_url")
        if name not in wanted:
            continue
        if name in result or type(size) is not int or size < 1:
            raise UpdateError("Invalid GitHub release asset")
        checked = _github_url(url)
        if urlparse(checked).path != prefix + name:
            raise UpdateError("Invalid GitHub asset URL")
        result[name] = {"name": name, "size": size, "browser_download_url": checked}
    if set(result) != wanted:
        raise UpdateError("GitHub release is missing signed update assets")
    if result[METADATA_NAME]["size"] > MAX_METADATA_BYTES or result[SIGNATURE_NAME]["size"] > MAX_SIGNATURE_BYTES or result[ARTIFACT_NAME]["size"] > MAX_ARTIFACT_BYTES:
        raise UpdateError("GitHub release asset is too large")
    return [result[name] for name in (METADATA_NAME, SIGNATURE_NAME, ARTIFACT_NAME)]


def select_release(releases: object, channel: str) -> dict:
    if channel not in {"stable", "nightly"} or not isinstance(releases, list) or len(releases) > 20:
        raise UpdateError("Invalid GitHub releases response")
    eligible = []
    for item in releases:
        if not isinstance(item, dict) or type(item.get("id")) is not int or type(item.get("tag_name")) is not str or type(item.get("draft")) is not bool or type(item.get("prerelease")) is not bool:
            raise UpdateError("Invalid GitHub release")
        if item["draft"] or item["prerelease"] != (channel == "nightly"):
            continue
        candidate = dict(item)
        candidate["assets"] = _assets(item.get("assets"), item["tag_name"])
        eligible.append(candidate)
    if not eligible:
        raise UpdateError("No suitable signed release is available")
    return eligible[0]


def discover(channel: str, opener=_open) -> dict:
    _github_url(API_URL.split("?", 1)[0], api=True)
    try:
        value = json.loads(_request(API_URL, MAX_API_BYTES, opener))
    except json.JSONDecodeError as exc:
        raise UpdateError("Invalid GitHub releases response") from exc
    return select_release(value, channel)


def verify_signature(statement: bytes, signature: bytes, public_key: bytes) -> bool:
    descriptors = []
    try:
        for name, data in (("update-key", public_key), ("update-signature", signature)):
            descriptor = os.memfd_create(name)
            os.write(descriptor, data)
            os.lseek(descriptor, 0, os.SEEK_SET)
            descriptors.append(descriptor)
        result = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-verify", f"/proc/self/fd/{descriptors[0]}", "-signature", f"/proc/self/fd/{descriptors[1]}"],
            input=statement, pass_fds=tuple(descriptors), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return result.returncode == 0
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _file_entry(item: object) -> dict:
    if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "mode"} or type(item.get("path")) is not str or type(item.get("size")) is not int or type(item.get("sha256")) is not str or type(item.get("mode")) is not int:
        raise UpdateError("Invalid signed package file")
    path = PurePosixPath(item["path"])
    allowed = (
        path.as_posix() in {
            "control-freak-release/deploy/control_freak",
            "control-freak-release/deploy/control-freak.pygame",
            "control-freak-release/deploy/install_batocera.py", "control-freak-release/deploy/release-public.pem",
            "control-freak-release/deploy/release-trust.json", "control-freak-release/deploy/release-trust.sig",
            "control-freak-release/assets/control-freak-image.png",
        }
        or re.fullmatch(r"control-freak-release/assets/control-freak-logo-[1-6]\.png", path.as_posix()) is not None
        or path.as_posix().startswith("control-freak-release/src/control_freak/")
    )
    if path.is_absolute() or len(path.parts) < 3 or any(part in {"", ".", ".."} for part in path.parts) or not allowed or not 0 <= item["size"] <= MAX_EXPANDED_BYTES or not SHA256.fullmatch(item["sha256"]) or item["mode"] not in {0o444, 0o644, 0o755} or path.as_posix() == "control-freak-release/deploy/control-freak.pygame" and item["mode"] != 0o644:
        raise UpdateError("Invalid signed package file")
    return item


def verify_metadata(statement: bytes, signature: bytes, public_key: bytes, assets: object, *, python_version: tuple[int, int] = sys.version_info[:2], batocera_version: str | None = None) -> dict:
    if len(statement) > MAX_METADATA_BYTES or len(signature) > MAX_SIGNATURE_BYTES or not verify_signature(statement, signature, public_key):
        raise UpdateError("Invalid update metadata signature")
    try:
        value = json.loads(statement)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Invalid signed update metadata") from exc
    if not isinstance(value, dict) or set(value) != {"version", "release_id", "channel", "sequence", "artifact", "compatibility", "notes", "runtime", "files"} or type(value.get("version")) is not int or value["version"] != 1 or type(value.get("release_id")) is not str or not RELEASE_ID.fullmatch(value["release_id"]) or value.get("channel") not in {"stable", "nightly"} or type(value.get("sequence")) is not int or value["sequence"] < 1 or type(value.get("notes")) is not str or len(value["notes"].encode()) > 16384:
        raise UpdateError("Invalid signed update metadata")
    if statement != _canonical(value):
        raise UpdateError("Update metadata is not canonical")
    artifact = value.get("artifact")
    compatibility = value.get("compatibility")
    runtime = value.get("runtime")
    if not isinstance(artifact, dict) or set(artifact) != {"filename", "size", "sha256"} or artifact.get("filename") != ARTIFACT_NAME or type(artifact.get("size")) is not int or not 1 <= artifact["size"] <= MAX_ARTIFACT_BYTES or type(artifact.get("sha256")) is not str or not SHA256.fullmatch(artifact["sha256"]):
        raise UpdateError("Invalid signed update artifact")
    if not isinstance(compatibility, dict) or compatibility.get("python") != ">=3.12,<3.15" or set(compatibility) != {"python", "batocera"} or not isinstance(compatibility.get("batocera"), list) or not compatibility["batocera"] or any(type(item) is not str for item in compatibility["batocera"]) or len(compatibility["batocera"]) != len(set(compatibility["batocera"])) or set(compatibility["batocera"]) - {"43", "44"}:
        raise UpdateError("Invalid update compatibility")
    if not (3, 12) <= python_version < (3, 15) or batocera_version not in compatibility["batocera"]:
        raise UpdateError("This release is not compatible with this system")
    if not isinstance(runtime, dict) or set(runtime) != {"trust_sha256"} or type(runtime.get("trust_sha256")) is not str or not SHA256.fullmatch(runtime["trust_sha256"]):
        raise UpdateError("Invalid runtime trust relationship")
    if not isinstance(value.get("files"), list) or not value["files"] or len(value["files"]) > 512:
        raise UpdateError("Invalid signed package files")
    files = [_file_entry(item) for item in value["files"]]
    paths = [item["path"] for item in files]
    if len(paths) != len(set(paths)) or sum(item["size"] for item in files) > MAX_EXPANDED_BYTES:
        raise UpdateError("Invalid signed package files")
    asset_map = {item["name"]: item for item in assets} if isinstance(assets, list) else {}
    if set(asset_map) != {METADATA_NAME, SIGNATURE_NAME, ARTIFACT_NAME} or asset_map[ARTIFACT_NAME].get("size") != artifact["size"]:
        raise UpdateError("Signed metadata does not match GitHub artifact")
    return value


def _batocera_version(path: Path = Path("/etc/os-release")) -> str | None:
    try:
        data = path.read_text()
        if len(data.encode()) > 65536:
            return None
        values = {}
        for line in data.splitlines():
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
                return None
            parsed = shlex.split(raw, comments=False, posix=True)
            if len(parsed) != 1 and not (not parsed and raw == ""):
                return None
            values[key] = parsed[0] if parsed else ""
    except (OSError, UnicodeError, ValueError):
        return None
    version = values.get("VERSION", "")
    if values.get("NAME") != "Batocera.linux" or values.get("ID") != "buildroot" or not re.fullmatch(r"(?:43|44)(?:\.[0-9]+)*", version):
        return None
    return version.split(".", 1)[0]


def download(url: str, size: int, sha256: str, opener=_open) -> bytes:
    _github_url(url)
    if type(size) is not int or not 1 <= size <= MAX_ARTIFACT_BYTES or not SHA256.fullmatch(sha256):
        raise UpdateError("Invalid download request")
    data = _request(url, size, opener)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise UpdateError("Downloaded artifact failed size or hash verification")
    return data


def extract_artifact(data: bytes, files: list[dict], destination: Path) -> Path:
    expected = {item["path"]: _file_entry(item) for item in files}
    if len(expected) != len(files) or sum(item["size"] for item in expected.values()) > MAX_EXPANDED_BYTES:
        raise UpdateError("Invalid signed package files")
    destination.mkdir(mode=0o700)
    try:
        # Parse from memory only after the bounded network read.
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise UpdateError("Artifact contains unexpected or duplicate files")
            for info in infos:
                item = expected[info.filename]
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or mode & 0o777 not in {0, item["mode"]} or info.file_size != item["size"] or info.compress_size > MAX_ARTIFACT_BYTES:
                    raise UpdateError("Artifact contains an unsafe file")
                target = destination.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = archive.read(info)
                if len(payload) != item["size"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise UpdateError("Artifact file failed verification")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item["mode"])
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                os.chmod(target, item["mode"])
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("Could not safely extract update artifact") from exc
    return destination / "control-freak-release"


def _asset(release: dict, name: str) -> dict:
    return next(item for item in release["assets"] if item["name"] == name)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sequence_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 2, "channels": {}, "pending": None}
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("Installed update sequence state is invalid") from exc
    channels = value.get("channels") if isinstance(value, dict) else None
    if not isinstance(channels, dict) or set(channels) - {"stable", "nightly"} or any(type(item) is not int or item < 1 for item in channels.values()):
        raise UpdateError("Installed update sequence state is invalid")
    if value.get("version") == 1 and set(value) == {"version", "channels"}:
        return {"version": 2, "channels": channels, "pending": None}
    pending = value.get("pending")
    if set(value) != {"version", "channels", "pending"} or value.get("version") != 2 or pending is not None and (
        not isinstance(pending, dict)
        or set(pending) != {"channel", "sequence", "metadata_sha256"}
        or pending.get("channel") not in {"stable", "nightly"}
        or type(pending.get("sequence")) is not int
        or pending["sequence"] < 1
        or pending["sequence"] != channels.get(pending["channel"])
        or type(pending.get("metadata_sha256")) is not str
        or not SHA256.fullmatch(pending["metadata_sha256"])
    ):
        raise UpdateError("Installed update sequence state is invalid")
    return value


def _reject_replay(path: Path, channel: str, sequence: int, statement: bytes) -> bool:
    state = _sequence_state(path)
    pending = state["pending"]
    if pending is not None:
        if pending == {"channel": channel, "sequence": sequence, "metadata_sha256": hashlib.sha256(statement).hexdigest()}:
            return True
        raise UpdateError("Update sequence is not newer than the highest installed release")
    if sequence <= state["channels"].get(channel, 0):
        raise UpdateError("Update sequence is not newer than the highest installed release")
    return False


@contextmanager
def _sequence_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UpdateError("Installed update sequence lock is invalid")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def check_for_update(channel: str, installed: str, public_key: bytes, state_path: Path, *, opener=_open, now: float | None = None) -> dict:
    release = discover(channel, opener)
    metadata_asset = _asset(release, METADATA_NAME)
    signature_asset = _asset(release, SIGNATURE_NAME)
    statement = _request(metadata_asset["browser_download_url"], metadata_asset["size"], opener)
    signature = _request(signature_asset["browser_download_url"], signature_asset["size"], opener)
    value = verify_metadata(statement, signature, public_key, release["assets"], batocera_version=_batocera_version())
    if value["release_id"] != release["tag_name"]:
        raise UpdateError("Signed release ID does not match the GitHub tag")
    if value["channel"] == "nightly" and not release["prerelease"] or value["channel"] == "stable" and release["prerelease"]:
        raise UpdateError("Signed channel does not match GitHub release")
    retry = _reject_replay(state_path.with_name(SEQUENCE_STATE), channel, value["sequence"], statement)
    available = retry or value["release_id"] != installed
    result = {"version": 2, "channel": channel, "installed": installed, "last_checked": int(time.time() if now is None else now), "available": available, "release_id": value["release_id"] if available else None, "notes": value["notes"] if available else ""}
    _write_json(state_path, result)
    return {**result, "_candidate": {"release": release, "metadata": value, "statement": statement, "signature": signature}}


def daily_check(channel: str, installed: str, public_key: bytes, state_path: Path, *, now: float | None = None, install=None, opener=_open) -> dict:
    del install  # Daily discovery never installs.
    current_time = int(time.time() if now is None else now)
    try:
        cached = json.loads(state_path.read_text())
        if set(cached) == {"version", "channel", "installed", "last_checked", "available", "release_id", "notes"} and cached["version"] == 2 and cached["channel"] == channel and cached["installed"] == installed and type(cached.get("last_checked")) is int and current_time - cached["last_checked"] < 86400:
            return cached
    except (OSError, json.JSONDecodeError):
        pass
    return check_for_update(channel, installed, public_key, state_path, opener=opener, now=current_time)


def install_candidate(candidate: dict, userdata: Path, notes_directory: Path, *, run=subprocess.run, opener=_open) -> str:
    sequence_path = userdata / "system/control-freak" / SEQUENCE_STATE
    with _sequence_lock(sequence_path):
        return _install_candidate_serialized(candidate, userdata, notes_directory, sequence_path, run=run, opener=opener)


def _install_candidate_serialized(candidate: dict, userdata: Path, notes_directory: Path, sequence_path: Path, *, run, opener) -> str:
    release = candidate.get("release")
    value = candidate.get("metadata")
    statement = candidate.get("statement")
    signature = candidate.get("signature")
    if not isinstance(release, dict) or not isinstance(value, dict) or not isinstance(statement, bytes) or not isinstance(signature, bytes):
        raise UpdateError("Invalid update candidate")
    _reject_replay(sequence_path, value["channel"], value["sequence"], statement)
    artifact = value["artifact"]
    asset = _asset(release, ARTIFACT_NAME)
    data = download(asset["browser_download_url"], artifact["size"], artifact["sha256"], opener)
    with tempfile.TemporaryDirectory(prefix="control-freak-update-") as temporary:
        source = extract_artifact(data, value["files"], Path(temporary))
        trust = source / "deploy/release-trust.json"
        try:
            trust_hash = hashlib.sha256(trust.read_bytes()).hexdigest()
        except OSError as exc:
            raise UpdateError("Update is missing runtime trust metadata") from exc
        if trust_hash != value["runtime"]["trust_sha256"]:
            raise UpdateError("Update runtime trust relationship does not match")
        notes_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for suffix, payload in (("json", statement), ("sig", signature)):
            target = notes_directory / f"{value['release_id']}.{suffix}"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        command = [sys.executable, str(source / "deploy/install_batocera.py"), "--root", str(userdata), "--source", str(source), "--release-id", value["release_id"], "--channel", value["channel"]]
        sequences = _sequence_state(sequence_path)
        pending = {"channel": value["channel"], "sequence": value["sequence"], "metadata_sha256": hashlib.sha256(statement).hexdigest()}
        if sequences["pending"] is None:
            sequences = {"version": 2, "channels": {**sequences["channels"], value["channel"]: value["sequence"]}, "pending": pending}
            _write_json(sequence_path, sequences)
        try:
            result = run(command, check=False, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Update installer could not run") from exc
        if result.returncode:
            raise UpdateError("Update installer failed safely")
    _write_json(sequence_path, {**sequences, "pending": None})
    return value["release_id"]


def cached_notes(release_id: str, public_key: bytes, notes_directory: Path) -> str:
    if not RELEASE_ID.fullmatch(release_id):
        raise UpdateError("Invalid installed release")
    try:
        statement = (notes_directory / f"{release_id}.json").read_bytes()
        signature = (notes_directory / f"{release_id}.sig").read_bytes()
        parsed = json.loads(statement)
        artifact = parsed["artifact"]
        batocera = parsed["compatibility"]["batocera"][0]
        assets = [{"name": METADATA_NAME}, {"name": SIGNATURE_NAME}, {"name": ARTIFACT_NAME, "size": artifact["size"]}]
        value = verify_metadata(statement, signature, public_key, assets, python_version=(3, 12), batocera_version=batocera)
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError("Verified offline release notes are unavailable") from exc
    if value["release_id"] != release_id:
        raise UpdateError("Verified offline release notes do not match installed release")
    return value["notes"]
