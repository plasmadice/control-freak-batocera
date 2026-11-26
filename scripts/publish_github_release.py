#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

NAMES = ("control-freak-release.json", "control-freak-release.sig", "control-freak-release.zip")
ASSETS = NAMES
PUBLIC_KEY = Path(__file__).resolve().parents[1] / "deploy/release-public.pem"
REPOSITORY = "plasmadice/control-freak-batocera"
MAX_API_BYTES = 512 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024


class PublishError(RuntimeError):
    pass


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _fetch(url: str, limit: int, opener) -> bytes:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "control-freak-batocera-publisher"})
    try:
        with opener(request, timeout=20) as response:
            if getattr(response, "status", 200) != 200:
                raise PublishError("published release could not be fetched anonymously")
            data = response.read(limit + 1)
    except PublishError:
        raise
    except (OSError, TimeoutError) as exc:
        raise PublishError("published release could not be fetched anonymously") from exc
    if len(data) > limit:
        raise PublishError("published release response is too large")
    return data


def _verify_published(tag: str, nightly: bool, expected: tuple[bytes, ...], run, opener) -> None:
    api = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{quote(tag, safe='')}"
    try:
        release = json.loads(_fetch(api, MAX_API_BYTES, opener))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("published release metadata is invalid") from exc
    if not isinstance(release, dict) or release.get("tag_name") != tag or release.get("draft") is not False or release.get("prerelease") is not nightly or not isinstance(release.get("assets"), list):
        raise PublishError("published release tag or prerelease flag does not match")
    assets = release["assets"]
    if len(assets) != len(NAMES) or any(not isinstance(item, dict) for item in assets):
        raise PublishError("published release asset list does not match")
    by_name = {item.get("name"): item for item in assets}
    if set(by_name) != set(NAMES):
        raise PublishError("published release asset list does not match")
    downloaded = []
    for name, wanted in zip(NAMES, expected, strict=True):
        item = by_name[name]
        url = item.get("browser_download_url")
        expected_url = f"https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe='')}/{name}"
        if type(item.get("size")) is not int or item["size"] != len(wanted) or url != expected_url:
            raise PublishError("published release asset metadata does not match")
        data = _fetch(url, min(MAX_ASSET_BYTES, len(wanted)), opener)
        if len(data) != len(wanted) or hashlib.sha256(data).digest() != hashlib.sha256(wanted).digest():
            raise PublishError("published release asset bytes do not match")
        downloaded.append(data)
    try:
        value = json.loads(downloaded[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("published release metadata is invalid") from exc
    if downloaded[0] != _canonical(value) or value.get("release_id") != tag or value.get("channel") != ("nightly" if nightly else "stable"):
        raise PublishError("published metadata tag or channel does not match")
    with tempfile.TemporaryDirectory(prefix="control-freak-published-") as temporary:
        paths = [Path(temporary) / name for name in NAMES]
        for path, data in zip(paths, downloaded, strict=True):
            path.write_bytes(data)
        try:
            verified = run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(PUBLIC_KEY), "-signature", str(paths[1]), str(paths[0])], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise PublishError("published metadata signature could not be verified") from exc
        if verified.returncode:
            raise PublishError("published metadata signature is invalid")


def publish(tag: str, directory: Path, nightly: bool, *, run=subprocess.run, opener=urlopen) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise PublishError("release directory is unavailable") from exc
    assets = [directory / name for name in NAMES]
    if directory.is_symlink() or {path.name for path in entries} != set(NAMES) or len(entries) != len(NAMES) or any(path.is_symlink() or not path.is_file() for path in assets):
        raise PublishError("directory must contain exactly three regular non-symlink release assets")
    try:
        statement = assets[0].read_bytes()
        value = json.loads(statement)
        artifact_data = assets[2].read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("release metadata is invalid") from exc
    if not isinstance(value, dict) or statement != _canonical(value):
        raise PublishError("release metadata is not canonical JSON")
    if value.get("release_id") != tag:
        raise PublishError("tag does not match signed release ID")
    if value.get("channel") != ("nightly" if nightly else "stable"):
        raise PublishError("signed channel does not match --nightly")
    artifact = value.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {"filename", "size", "sha256"} or artifact.get("filename") != NAMES[2] or type(artifact.get("size")) is not int or artifact["size"] != len(artifact_data) or artifact.get("sha256") != hashlib.sha256(artifact_data).hexdigest():
        raise PublishError("signed artifact metadata does not match release artifact")
    if PUBLIC_KEY.is_symlink() or not PUBLIC_KEY.is_file():
        raise PublishError("release public key is unavailable")
    snapshot_data = (statement, assets[1].read_bytes(), artifact_data)
    with tempfile.TemporaryDirectory(prefix="control-freak-publish-") as temporary:
        snapshot = [Path(temporary) / name for name in NAMES]
        for path, data in zip(snapshot, snapshot_data, strict=True):
            path.write_bytes(data)
        try:
            verified = run(
                ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(PUBLIC_KEY), "-signature", str(snapshot[1]), str(snapshot[0])],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise PublishError("release signature could not be verified") from exc
        if verified.returncode:
            raise PublishError("release signature is invalid")
        command = ["gh", "release", "create", tag, *map(str, snapshot), "--repo", "plasmadice/control-freak-batocera", "--verify-tag", "--title", tag, "--notes", "See authenticated in-app release notes."]
        if nightly:
            command.append("--prerelease")
        run(command, check=True)
        _verify_published(tag, nightly, snapshot_data, run, opener)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish already-signed assets to the public GitHub mirror")
    parser.add_argument("tag")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--nightly", action="store_true")
    args = parser.parse_args()
    try:
        publish(args.tag, args.directory, args.nightly)
    except PublishError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
