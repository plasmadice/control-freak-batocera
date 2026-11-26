import hashlib
import io
import json
from pathlib import Path
import subprocess

import pytest

from scripts import publish_github_release as publisher


def canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def release_assets(directory: Path, *, channel: str = "stable") -> dict:
    directory.mkdir()
    artifact = b"release artifact"
    value = {
        "release_id": "1.2.3",
        "channel": channel,
        "artifact": {
            "filename": "control-freak-release.zip",
            "size": len(artifact),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        },
    }
    (directory / "control-freak-release.json").write_bytes(canonical(value))
    (directory / "control-freak-release.sig").write_bytes(b"signature")
    (directory / "control-freak-release.zip").write_bytes(artifact)
    return value


def anonymous_opener(directory: Path, *, tag: str = "1.2.3", reported_tag: str | None = None, nightly: bool = False, replace: dict[str, bytes] | None = None, requests: list | None = None):
    payloads = {name: (directory / name).read_bytes() for name in publisher.NAMES}
    payloads.update(replace or {})
    api = f"https://api.github.com/repos/plasmadice/control-freak-batocera/releases/tags/{tag}"
    urls = {name: f"https://github.com/plasmadice/control-freak-batocera/releases/download/{tag}/{name}" for name in publisher.NAMES}
    release = json.dumps({
        "tag_name": tag if reported_tag is None else reported_tag,
        "draft": False,
        "prerelease": nightly,
        "assets": [{"name": name, "size": len(payloads[name]), "browser_download_url": urls[name]} for name in publisher.NAMES],
    }).encode()

    class Response(io.BytesIO):
        status = 200
        headers = {}

        def __init__(self, data: bytes, url: str):
            super().__init__(data)
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def geturl(self):
            return self.url

    def open_request(request, **_kwargs):
        if requests is not None:
            requests.append(request)
        assert request.get_header("Authorization") is None
        if request.full_url == api:
            return Response(release, api)
        name = next(name for name, url in urls.items() if url == request.full_url)
        return Response(payloads[name], urls[name])

    return open_request


def test_publish_validates_signature_before_invoking_gh(tmp_path: Path) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    requests = []
    publisher.publish("1.2.3", directory, False, run=run, opener=anonymous_opener(directory, requests=requests))

    assert calls[0][0][:5] == [
        "/usr/bin/openssl", "dgst", "-sha256", "-verify", str(publisher.PUBLIC_KEY),
    ]
    assert Path(calls[0][0][6]).name == "control-freak-release.sig"
    assert Path(calls[0][0][7]).name == "control-freak-release.json"
    assert Path(calls[0][0][6]).parent != directory
    assert calls[1][0][:4] == ["gh", "release", "create", "1.2.3"]
    assert calls[1][1]["check"] is True
    assert len(requests) == 4


@pytest.mark.parametrize("problem", ["extra", "symlink", "noncanonical", "tag", "channel", "filename", "size", "sha256"])
def test_publish_rejects_invalid_assets_before_invoking_gh(tmp_path: Path, problem: str) -> None:
    directory = tmp_path / "release"
    value = release_assets(directory)
    tag = "1.2.3"
    nightly = False
    if problem == "extra":
        (directory / "credentials.pem").write_text("secret")
    elif problem == "symlink":
        signature = directory / "control-freak-release.sig"
        signature.unlink()
        signature.symlink_to(directory / "control-freak-release.json")
    elif problem == "noncanonical":
        (directory / "control-freak-release.json").write_text(json.dumps(value))
    elif problem == "tag":
        tag = "2.0.0"
    elif problem == "channel":
        nightly = True
    else:
        if problem == "filename":
            value["artifact"]["filename"] = "other.zip"
        elif problem == "size":
            value["artifact"]["size"] += 1
        else:
            value["artifact"]["sha256"] = "0" * 64
        (directory / "control-freak-release.json").write_bytes(canonical(value))
    calls = []

    with pytest.raises(publisher.PublishError):
        publisher.publish(tag, directory, nightly, run=lambda command, **kwargs: calls.append(command))

    assert not any(command[0] == "gh" for command in calls)


def test_publish_rejects_invalid_detached_signature_without_invoking_gh(tmp_path: Path) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(publisher.PublishError, match="signature"):
        publisher.publish("1.2.3", directory, False, run=run)

    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/openssl"


def test_publish_uploads_private_verified_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    uploaded = {}

    def run(command, **_kwargs):
        if command[0] == "gh":
            for name in publisher.ASSETS:
                (directory / name).write_bytes(b"replaced")
            for argument in command:
                path = Path(argument)
                if path.name in publisher.ASSETS:
                    uploaded[path.name] = path.read_bytes()
        return subprocess.CompletedProcess(command, 0)

    publisher.publish("1.2.3", directory, False, run=run, opener=anonymous_opener(directory))

    assert uploaded["control-freak-release.zip"] == b"release artifact"
    assert uploaded["control-freak-release.sig"] == b"signature"
    assert uploaded["control-freak-release.json"] != b"replaced"


def test_publish_rejects_anonymously_fetched_asset_mismatch_after_create(tmp_path: Path) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    opener = anonymous_opener(directory, replace={"control-freak-release.zip": b"release artifacx"})
    with pytest.raises(publisher.PublishError, match="asset"):
        publisher.publish("1.2.3", directory, False, run=run, opener=opener)

    assert any(command[:3] == ["gh", "release", "create"] for command in calls)


@pytest.mark.parametrize("opener_kwargs", [{"nightly": True}, {"reported_tag": "other"}])
def test_publish_rejects_remote_prerelease_or_tag_mismatch_after_create(tmp_path: Path, opener_kwargs: dict) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(publisher.PublishError, match="tag or prerelease"):
        publisher.publish("1.2.3", directory, False, run=run, opener=anonymous_opener(directory, **opener_kwargs))

    assert any(command[:3] == ["gh", "release", "create"] for command in calls)


def test_publish_reverifies_downloaded_metadata_signature(tmp_path: Path) -> None:
    directory = tmp_path / "release"
    release_assets(directory)
    openssl_calls = 0

    def run(command, **_kwargs):
        nonlocal openssl_calls
        if command[0] == "/usr/bin/openssl":
            openssl_calls += 1
            return subprocess.CompletedProcess(command, 1 if openssl_calls == 2 else 0)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(publisher.PublishError, match="published metadata signature"):
        publisher.publish("1.2.3", directory, False, run=run, opener=anonymous_opener(directory))
