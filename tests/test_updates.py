import hashlib
import io
import json
import subprocess
import sys
import warnings
from pathlib import Path
import zipfile

import pytest

from control_freak import updates


def canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def metadata(files: list[dict] | None = None, *, channel: str = "stable", sequence: int = 1) -> dict:
    return {
        "version": 1,
        "release_id": "1.2.3",
        "channel": channel,
        "sequence": sequence,
        "artifact": {"filename": "control-freak-release.zip", "size": 123, "sha256": "a" * 64},
        "compatibility": {"python": ">=3.12,<3.15", "batocera": ["43", "44"]},
        "notes": "Signed release notes.",
        "runtime": {"trust_sha256": "b" * 64},
        "files": files or [{"path": "control-freak-release/deploy/install_batocera.py", "size": 4, "sha256": "c" * 64, "mode": 0o644}],
    }


def release(tag: str, prerelease: bool, base: str = "https://github.com/plasmadice/control-freak-batocera/releases/download") -> dict:
    return {
        "id": 7,
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {"name": name, "size": size, "browser_download_url": f"{base}/{tag}/{name}"}
            for name, size in ((updates.METADATA_NAME, 100), (updates.SIGNATURE_NAME, 384), (updates.ARTIFACT_NAME, 123))
        ],
    }


def test_discovery_uses_only_expected_public_github_release_assets() -> None:
    stable = release("v1.2.3", False)
    nightly = release("nightly-20261126", True)

    assert updates.select_release([nightly, stable], "stable")["tag_name"] == "v1.2.3"
    assert updates.select_release([stable, nightly], "nightly")["tag_name"] == "nightly-20261126"

    poisoned = release("v1.2.3", False, "https://evil.example/releases/download")
    with pytest.raises(updates.UpdateError, match="asset URL"):
        updates.select_release([poisoned], "stable")


def test_nightly_discovery_uses_prereleases_only() -> None:
    stable = release("v1.2.3", False)
    nightly = release("nightly-20261126", True)

    with pytest.raises(updates.UpdateError, match="No suitable"):
        updates.select_release([stable], "nightly")

    stable["assets"][0]["browser_download_url"] = "https://evil.example/metadata"
    assert updates.select_release([stable, nightly], "nightly")["tag_name"] == "nightly-20261126"


def test_api_response_allows_only_the_exact_releases_endpoint() -> None:
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "2"}

        def __init__(self, url: str):
            super().__init__(b"[]")
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    assert updates._request(
        updates.API_URL, updates.MAX_API_BYTES,
        opener=lambda *_args, **_kwargs: Response(updates.API_URL),
    ) == b"[]"
    with pytest.raises(updates.UpdateError, match="API URL"):
        updates._request(
            updates.API_URL, updates.MAX_API_BYTES,
            opener=lambda *_args, **_kwargs: Response("https://api.github.com/user"),
        )


def test_signed_metadata_is_canonical_exact_and_binds_api_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    value = metadata()
    statement = canonical(value)
    monkeypatch.setattr(updates, "verify_signature", lambda data, signature, key: signature == b"signature")

    parsed = updates.verify_metadata(statement, b"signature", b"key", release("v1.2.3", False)["assets"], python_version=(3, 12), batocera_version="43")
    assert parsed["notes"] == "Signed release notes."

    with pytest.raises(updates.UpdateError, match="canonical"):
        updates.verify_metadata(json.dumps(value).encode(), b"signature", b"key", release("v1.2.3", False)["assets"], python_version=(3, 12), batocera_version="43")
    with pytest.raises(updates.UpdateError, match="artifact"):
        changed = release("v1.2.3", False)
        changed["assets"][2]["size"] = 124
        updates.verify_metadata(statement, b"signature", b"key", changed["assets"], python_version=(3, 12), batocera_version="43")


def test_signed_metadata_accepts_target_python_314_and_rejects_315(monkeypatch: pytest.MonkeyPatch) -> None:
    value = metadata()
    statement = canonical(value)
    monkeypatch.setattr(updates, "verify_signature", lambda *_args: True)

    assert updates.verify_metadata(
        statement, b"signature", b"key", release("v1.2.3", False)["assets"],
        python_version=(3, 14), batocera_version="43",
    )["release_id"] == "1.2.3"
    with pytest.raises(updates.UpdateError, match="not compatible"):
        updates.verify_metadata(
            statement, b"signature", b"key", release("v1.2.3", False)["assets"],
            python_version=(3, 15), batocera_version="43",
        )


def test_signed_metadata_rejects_indeterminate_batocera_version(monkeypatch: pytest.MonkeyPatch) -> None:
    value = metadata()
    statement = canonical(value)
    monkeypatch.setattr(updates, "verify_signature", lambda *_args: True)

    with pytest.raises(updates.UpdateError, match="not compatible"):
        updates.verify_metadata(
            statement, b"signature", b"key", release("v1.2.3", False)["assets"],
            python_version=(3, 12), batocera_version=None,
        )


@pytest.mark.parametrize("sequence", [0, -1, True])
def test_signed_metadata_requires_positive_sequence(monkeypatch: pytest.MonkeyPatch, sequence: int) -> None:
    value = metadata(sequence=sequence)
    monkeypatch.setattr(updates, "verify_signature", lambda *_args: True)

    with pytest.raises(updates.UpdateError, match="metadata"):
        updates.verify_metadata(canonical(value), b"signature", b"key", release("v1.2.3", False)["assets"], python_version=(3, 12), batocera_version="43")


@pytest.mark.parametrize("batocera", [[], ["42"], ["45"], ["43", "43"], ["43", "44", "45"]])
def test_signed_metadata_requires_supported_deduplicated_batocera_subset(monkeypatch: pytest.MonkeyPatch, batocera: list[str]) -> None:
    value = metadata()
    value["compatibility"]["batocera"] = batocera
    monkeypatch.setattr(updates, "verify_signature", lambda *_args: True)

    with pytest.raises(updates.UpdateError, match="compatibility"):
        updates.verify_metadata(canonical(value), b"signature", b"key", release("v1.2.3", False)["assets"], python_version=(3, 12), batocera_version="43")


def test_signed_package_rejects_obsolete_rom_launcher() -> None:
    with pytest.raises(updates.UpdateError, match="Invalid signed package file"):
        updates._file_entry({
            "path": "control-freak-release/deploy/control-freak.pygame",
            "size": 1,
            "sha256": "a" * 64,
            "mode": 0o755,
        })


def test_safe_extract_rejects_traversal_links_duplicates_and_unlisted_files(tmp_path: Path) -> None:
    def archive(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
        stream = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(stream, "w") as bundle:
                for name, data in entries:
                    info = name if isinstance(name, zipfile.ZipInfo) else zipfile.ZipInfo(name)
                    if not info.external_attr:
                        info.external_attr = (0o100644 << 16)
                    bundle.writestr(info, data)
        return stream.getvalue()

    good_data = b"data"
    item = {"path": "control-freak-release/deploy/install_batocera.py", "size": len(good_data), "sha256": hashlib.sha256(good_data).hexdigest(), "mode": 0o644}
    destination = tmp_path / "good"
    updates.extract_artifact(archive([(item["path"], good_data)]), [item], destination)
    assert (destination / item["path"]).read_bytes() == good_data

    link = zipfile.ZipInfo(item["path"])
    link.external_attr = (0o120777 << 16)
    cases: list[list[tuple[str | zipfile.ZipInfo, bytes]]] = [
        [("../escape", b"x")],
        [(item["path"], good_data), (item["path"], good_data)],
        [(item["path"], good_data), ("control-freak-release/extra", b"x")],
        [(link, good_data)],
    ]
    for index, entries in enumerate(cases):
        with pytest.raises(updates.UpdateError):
            updates.extract_artifact(archive(entries), [item], tmp_path / f"bad-{index}")


def test_stream_download_enforces_size_hash_and_redirect_policy() -> None:
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "4"}
        url = "https://objects.githubusercontent.com/object"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    digest = hashlib.sha256(b"data").hexdigest()
    assert updates.download("https://github.com/plasmadice/control-freak-batocera/releases/download/v1/a", 4, digest, opener=lambda *_args, **_kwargs: Response(b"data")) == b"data"
    with pytest.raises(updates.UpdateError):
        updates.download("https://github.com/plasmadice/control-freak-batocera/releases/download/v1/a", 3, digest, opener=lambda *_args, **_kwargs: Response(b"data"))


def test_manual_check_is_fresh_and_daily_check_only_marks_availability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    candidate = release("1.2.3", False)
    statement = canonical(metadata())
    monkeypatch.setattr(updates, "discover", lambda channel, opener=None: calls.append(channel) or candidate)
    monkeypatch.setattr(updates, "_request", lambda url, limit, opener=None: statement if url.endswith(updates.METADATA_NAME) else b"signature")
    monkeypatch.setattr(updates, "verify_metadata", lambda *args, **kwargs: metadata())

    result = updates.check_for_update("stable", "1.0.0", b"key", tmp_path / "updates.json")
    assert result["version"] == 2
    assert result["available"] is True
    assert result["release_id"] == "1.2.3"
    assert calls == ["stable"]

    installed = []
    assert updates.daily_check("stable", "1.0.0", b"key", tmp_path / "updates.json", now=100_000, install=lambda *_args: installed.append(True))["available"] is True
    assert installed == []


def test_daily_cache_is_keyed_by_channel_and_installed_release_and_ignores_old_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "updates.json"
    old = {"version": 1, "last_checked": 99_999, "available": False, "release_id": None, "notes": ""}
    state.write_text(json.dumps(old))
    calls = []

    def check(channel, installed, *_args, **_kwargs):
        calls.append((channel, installed))
        return {**old, "version": 2, "channel": channel, "installed": installed}

    monkeypatch.setattr(updates, "check_for_update", check)
    updates.daily_check("stable", "1.0.0", b"key", state, now=100_000)
    assert calls == [("stable", "1.0.0")]

    current = {**old, "version": 2, "channel": "stable", "installed": "1.0.0"}
    state.write_text(json.dumps(current))
    assert updates.daily_check("stable", "1.0.0", b"key", state, now=100_000) == current
    assert calls == [("stable", "1.0.0")]

    updates.daily_check("nightly", "1.0.0", b"key", state, now=100_000)
    updates.daily_check("stable", "2.0.0", b"key", state, now=100_000)
    assert calls[-2:] == [("nightly", "1.0.0"), ("stable", "2.0.0")]


def test_signed_release_id_must_match_github_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = release("different-tag", False)
    statement = canonical(metadata())
    monkeypatch.setattr(updates, "discover", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(updates, "_request", lambda url, *_args, **_kwargs: statement if url.endswith(updates.METADATA_NAME) else b"signature")
    monkeypatch.setattr(updates, "verify_metadata", lambda *_args, **_kwargs: metadata())

    with pytest.raises(updates.UpdateError, match="tag"):
        updates.check_for_update("stable", "1.0.0", b"key", tmp_path / "updates.json")


@pytest.mark.parametrize("sequence", [6, 7])
def test_discovery_rejects_lower_or_equal_installed_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sequence: int) -> None:
    state = tmp_path / "updates.json"
    (tmp_path / updates.SEQUENCE_STATE).write_text(json.dumps({"version": 1, "channels": {"stable": 7}}))
    candidate = release("1.2.3", False)
    monkeypatch.setattr(updates, "discover", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(updates, "_request", lambda url, *_args, **_kwargs: canonical(metadata(sequence=sequence)) if url.endswith(updates.METADATA_NAME) else b"signature")
    monkeypatch.setattr(updates, "verify_metadata", lambda *_args, **_kwargs: metadata(sequence=sequence))

    with pytest.raises(updates.UpdateError, match="sequence"):
        updates.check_for_update("stable", "1.0.0", b"key", state)


def test_discovery_allows_exact_pending_candidate_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "updates.json"
    value = metadata(sequence=7)
    statement = canonical(value)
    (tmp_path / updates.SEQUENCE_STATE).write_text(json.dumps({
        "version": 2,
        "channels": {"stable": 7},
        "pending": {"channel": "stable", "sequence": 7, "metadata_sha256": hashlib.sha256(statement).hexdigest()},
    }))
    monkeypatch.setattr(updates, "discover", lambda *_args, **_kwargs: release("1.2.3", False))
    monkeypatch.setattr(updates, "_request", lambda url, *_args, **_kwargs: statement if url.endswith(updates.METADATA_NAME) else b"signature")
    monkeypatch.setattr(updates, "verify_metadata", lambda *_args, **_kwargs: value)

    result = updates.check_for_update("stable", "1.2.3", b"key", state)
    assert result["available"] is True
    assert result["_candidate"]["statement"] == statement


def test_install_downloads_verifies_extracts_and_invokes_lifecycle_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust = b"signed runtime trust\n"
    value = metadata([
        {"path": "control-freak-release/deploy/install_batocera.py", "size": 4, "sha256": "c" * 64, "mode": 0o644},
        {"path": "control-freak-release/deploy/release-trust.json", "size": len(trust), "sha256": hashlib.sha256(trust).hexdigest(), "mode": 0o444},
    ])
    value["runtime"]["trust_sha256"] = hashlib.sha256(trust).hexdigest()
    candidate = {"release": release("v1.2.3", False), "metadata": value, "statement": canonical(value), "signature": b"signature"}
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "deploy/release-trust.json").write_bytes(trust)
    monkeypatch.setattr(updates, "download", lambda *_args, **_kwargs: b"artifact")
    monkeypatch.setattr(updates, "verify_signature", lambda *_args: True)
    monkeypatch.setattr(updates, "extract_artifact", lambda *_args, **_kwargs: source)
    commands = []

    result = updates.install_candidate(candidate, tmp_path / "userdata", tmp_path / "notes", run=lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0))

    assert result == "1.2.3"
    assert commands == [[sys.executable, str(source / "deploy/install_batocera.py"), "--root", str(tmp_path / "userdata"), "--source", str(source), "--release-id", "1.2.3", "--channel", "stable"]]
    assert updates.cached_notes("1.2.3", b"key", tmp_path / "notes") == "Signed release notes."
    sequence_path = tmp_path / "userdata/system/control-freak" / updates.SEQUENCE_STATE
    assert json.loads(sequence_path.read_text()) == {"version": 2, "channels": {"stable": 1}, "pending": None}
    with pytest.raises(updates.UpdateError, match="sequence"):
        updates.install_candidate(candidate, tmp_path / "userdata", tmp_path / "notes", run=lambda *_args, **_kwargs: pytest.fail("installer reran"))


def test_install_crash_after_success_keeps_ceiling_and_allows_only_exact_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust = b"signed runtime trust\n"
    value = metadata([
        {"path": "control-freak-release/deploy/install_batocera.py", "size": 4, "sha256": "c" * 64, "mode": 0o644},
        {"path": "control-freak-release/deploy/release-trust.json", "size": len(trust), "sha256": hashlib.sha256(trust).hexdigest(), "mode": 0o444},
    ], sequence=2)
    value["runtime"]["trust_sha256"] = hashlib.sha256(trust).hexdigest()
    candidate = {"release": release("v1.2.3", False), "metadata": value, "statement": canonical(value), "signature": b"signature"}
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "deploy/release-trust.json").write_bytes(trust)
    monkeypatch.setattr(updates, "download", lambda *_args, **_kwargs: b"artifact")
    monkeypatch.setattr(updates, "extract_artifact", lambda *_args, **_kwargs: source)
    real_write = updates._write_json
    writes = 0

    def crash_before_finalize(path: Path, state: dict) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise SystemExit("crash after installer success")
        real_write(path, state)

    monkeypatch.setattr(updates, "_write_json", crash_before_finalize)
    with pytest.raises(SystemExit, match="crash after installer success"):
        updates.install_candidate(candidate, tmp_path / "userdata", tmp_path / "notes", run=lambda command, **kwargs: subprocess.CompletedProcess(command, 0))

    sequence_path = tmp_path / "userdata/system/control-freak" / updates.SEQUENCE_STATE
    pending = json.loads(sequence_path.read_text())
    assert pending["channels"] == {"stable": 2}
    assert pending["pending"]["sequence"] == 2
    changed = {**candidate, "statement": candidate["statement"] + b" "}
    with pytest.raises(updates.UpdateError, match="sequence"):
        updates.install_candidate(changed, tmp_path / "userdata", tmp_path / "notes", run=lambda *_args, **_kwargs: pytest.fail("installer reran"))

    monkeypatch.setattr(updates, "_write_json", real_write)
    assert updates.install_candidate(candidate, tmp_path / "userdata", tmp_path / "notes", run=lambda command, **kwargs: subprocess.CompletedProcess(command, 0)) == "1.2.3"
    assert json.loads(sequence_path.read_text()) == {"version": 2, "channels": {"stable": 2}, "pending": None}
