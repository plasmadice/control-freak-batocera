import json
from pathlib import Path
import tomllib
import zipfile

import pytest

from scripts.build_release import build_release
from control_freak import __version__


def test_project_and_runtime_versions_match_release() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert project["project"]["version"] == "0.1.8"
    assert __version__ == "0.1.8"


def release_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / "src/control_freak").mkdir(parents=True)
    required = {
        "deploy/control_freak": b"service",
        "deploy/control-freak.pygame": b"launcher",
        "deploy/install_batocera.py": b"installer",
        "deploy/release-public.pem": b"key",
        "deploy/release-trust.json": b"trust",
        "deploy/release-trust.sig": b"signature",
        "assets/control-freak-image.png": b"image",
        **{f"assets/control-freak-logo-{number}.png": b"logo" for number in range(1, 7)},
        **{f"src/control_freak/{name}.py": b"" for name in (
            "__init__", "app", "audio", "input", "model", "preferences", "runtime",
            "service", "stateio", "text", "ui", "updates", "web",
        )},
        "src/control_freak/assets/fonts/DejaVuSans.ttf": b"font",
        "src/control_freak/assets/fonts/DejaVuSans-Bold.ttf": b"font",
        "src/control_freak/assets/fonts/LICENSE.txt": b"license",
    }
    for name, data in required.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return source


def test_release_builder_is_deterministic_and_emits_signable_canonical_metadata(tmp_path: Path) -> None:
    source = release_source(tmp_path)

    first = build_release(source, tmp_path / "one", "1.2.3", "stable", ["43", "44"], 9, "Notes")
    second = build_release(source, tmp_path / "two", "1.2.3", "stable", ["43", "44"], 9, "Notes")

    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes()
    value = json.loads(first[1].read_text())
    assert value["sequence"] == 9
    assert value["runtime"]["trust_sha256"]
    with zipfile.ZipFile(first[0]) as archive:
        assert set(archive.namelist()) == {item["path"] for item in value["files"]}
        assert "control-freak-release/deploy/control-freak.pygame" in archive.namelist()


def test_release_builder_rejects_unexpected_package_file(tmp_path: Path) -> None:
    source = release_source(tmp_path)
    (source / "src/control_freak/credentials.pem").write_text("secret")

    with pytest.raises(RuntimeError, match="unexpected"):
        build_release(source, tmp_path / "output", "1.2.3", "stable", ["43"], 1, "Notes")


def test_release_builder_ignores_python_bytecode_cache(tmp_path: Path) -> None:
    source = release_source(tmp_path)
    cache = source / "src/control_freak/__pycache__"
    cache.mkdir()
    (cache / "app.cpython-314.pyc").write_bytes(b"local build cache")

    artifact, metadata = build_release(source, tmp_path / "output", "1.2.3", "stable", ["43"], 1, "Notes")

    assert artifact.is_file()
    assert metadata.is_file()


def test_release_builder_rejects_package_symlink(tmp_path: Path) -> None:
    source = release_source(tmp_path)
    (source / "src/control_freak/link.py").symlink_to(source / "src/control_freak/app.py")

    with pytest.raises(RuntimeError, match="unexpected"):
        build_release(source, tmp_path / "output", "1.2.3", "stable", ["43"], 1, "Notes")


@pytest.mark.parametrize("batocera", [[], ["42"], ["45"], ["43", "43"], ["43", "44", "45"]])
def test_release_builder_rejects_invalid_batocera_compatibility(tmp_path: Path, batocera: list[str]) -> None:
    with pytest.raises(ValueError, match="metadata"):
        build_release(release_source(tmp_path), tmp_path / "output", "1.2.3", "stable", batocera, 1, "Notes")


@pytest.mark.parametrize("sequence", [0, -1, True])
def test_release_builder_requires_positive_sequence(tmp_path: Path, sequence: int) -> None:
    with pytest.raises(ValueError, match="metadata"):
        build_release(release_source(tmp_path), tmp_path / "output", "1.2.3", "stable", ["43"], sequence, "Notes")
