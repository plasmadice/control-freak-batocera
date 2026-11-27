#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ARTIFACT = "control-freak-release.zip"
METADATA = "control-freak-release.json"
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PACKAGE_FILES = (
    "__init__.py", "app.py", "audio.py", "input.py", "model.py", "preferences.py", "runtime.py",
    "service.py", "stateio.py", "text.py", "ui.py", "updates.py", "web.py",
    "assets/fonts/DejaVuSans.ttf", "assets/fonts/DejaVuSans-Bold.ttf", "assets/fonts/LICENSE.txt",
)


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sources(source: Path) -> list[Path]:
    fixed = [
        "deploy/control_freak", "deploy/control-freak.pygame", "deploy/install_batocera.py",
        "deploy/release-public.pem", "deploy/release-trust.json", "deploy/release-trust.sig",
        "assets/control-freak-image.png", *[f"assets/control-freak-logo-{number}.png" for number in range(1, 7)],
    ]
    package = source / "src/control_freak"
    actual = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if (path.is_file() or path.is_symlink())
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    if actual - set(PACKAGE_FILES):
        raise RuntimeError("release source contains an unexpected package file")
    paths = [source / name for name in fixed]
    paths.extend(package / name for name in PACKAGE_FILES)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise RuntimeError("release source is incomplete or contains a symlink")
    return paths


def build_release(source: Path, output: Path, release_id: str, channel: str, batocera: list[str], sequence: int, notes: str) -> tuple[Path, Path]:
    if not RELEASE_ID.fullmatch(release_id) or channel not in {"stable", "nightly"} or not batocera or len(batocera) != len(set(batocera)) or set(batocera) - {"43", "44"} or type(sequence) is not int or sequence < 1 or len(notes.encode()) > 16384:
        raise ValueError("invalid release metadata")
    source = source.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / ARTIFACT
    files = []
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _sources(source):
            relative = path.relative_to(source).as_posix()
            name = f"control-freak-release/{relative}"
            data = path.read_bytes()
            mode = 0o755 if relative == "deploy/control_freak" else 0o444 if relative in {"deploy/release-trust.json", "deploy/release-trust.sig"} else 0o644
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100000 | mode) << 16
            info.create_system = 3
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            files.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": mode})
    artifact_data = artifact.read_bytes()
    trust = (source / "deploy/release-trust.json").read_bytes()
    value = {
        "version": 1,
        "release_id": release_id,
        "channel": channel,
        "sequence": sequence,
        "artifact": {"filename": ARTIFACT, "size": len(artifact_data), "sha256": hashlib.sha256(artifact_data).hexdigest()},
        "compatibility": {"python": ">=3.12,<3.15", "batocera": batocera},
        "notes": notes,
        "runtime": {"trust_sha256": hashlib.sha256(trust).hexdigest()},
        "files": files,
    }
    metadata = output / METADATA
    metadata.write_bytes(_canonical(value))
    return artifact, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic signed-update inputs")
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--channel", choices=("stable", "nightly"), required=True)
    parser.add_argument("--batocera", nargs="+", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    artifact, metadata = build_release(args.source, args.output, args.release_id, args.channel, args.batocera, args.sequence, args.notes_file.read_text())
    print(artifact)
    print(metadata)


if __name__ == "__main__":
    main()
