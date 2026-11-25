import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET

import pytest

import deploy.install_batocera as installer
from deploy.install_batocera import install, uninstall


@pytest.fixture
def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def app(root: Path) -> Path:
    return root / "roms/pygame/control-freak"


def current(root: Path) -> dict:
    return json.loads((app(root) / "current.json").read_text())


def registered(root: Path) -> bool:
    games = ET.parse(root / "roms/pygame/gamelist.xml").getroot().findall("game")
    return any(game.findtext("path") == installer.GAME_PATH for game in games)


def copied_source(tmp_path: Path, source_root: Path) -> Path:
    target = tmp_path / "source"
    shutil.copytree(source_root, target, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))
    return target


def test_install_registers_versioned_pygame_application(tmp_path: Path, source_root: Path) -> None:
    operations = install(tmp_path, source_root, "0.1.0-test", "stable")

    root = app(tmp_path)
    assert (root / "control-freak.pygame").stat().st_mode & 0o777 == 0o755
    assert (root / "image.png").is_file()
    assert (root / "releases/0.1.0-test/control_freak/app.py").is_file()
    assert current(tmp_path) == {"channel": "stable", "release_id": "0.1.0-test", "version": 1}
    assert (root / "install_batocera.py").is_file()
    launcher_text = (root / "control-freak.pygame").read_text()
    assert "current.json" in launcher_text
    assert '"releases"' in launcher_text
    assert "sys.dont_write_bytecode = True" in launcher_text
    assert "install release 0.1.0-test" in operations
    assert registered(tmp_path)


def test_install_manages_logo_proposals(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    root = app(tmp_path)
    manifest = json.loads((root / "install-manifest.json").read_text())

    assert manifest["static"].keys() >= {
        "logo-proposal-1.png",
        "logo-proposal-2.png",
        "logo-proposal-3.png",
    }
    assert (root / "image.png").read_bytes() == (root / "logo-proposal-1.png").read_bytes()


def test_release_source_rejects_symlink_to_external_file(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path, source_root)
    external = tmp_path / "external.py"
    external.write_text("outside")
    (source / "src/control_freak/external.py").symlink_to(external)

    with pytest.raises(RuntimeError, match="Release source contains a symlink"):
        install(tmp_path / "root", source, "one", "stable")

    assert not app(tmp_path / "root").exists()


@pytest.mark.parametrize("relative", [False, True])
def test_generated_launcher_uses_selected_root(
    tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    source = copied_source(tmp_path, source_root)
    package = source / "src/control_freak"
    shutil.rmtree(package)
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "app.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def run(): Path(os.environ['CONTROL_FREAK_TEST_MARKER']).write_text('selected')\n"
    )
    if relative:
        monkeypatch.chdir(tmp_path)
        selected_root = Path("relative-root")
        installed_root = tmp_path / selected_root
    else:
        selected_root = tmp_path / "absolute-root"
        installed_root = selected_root
    install(selected_root, source, "selected", "stable")
    marker = tmp_path / f"marker-{relative}"
    environment = os.environ | {"CONTROL_FREAK_TEST_MARKER": str(marker)}

    completed = subprocess.run(
        [sys.executable, app(installed_root) / "control-freak.pygame"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text() == "selected"
    assert str(app(installed_root).absolute()) in (app(installed_root) / "control-freak.pygame").read_text()


def test_predictable_gamelist_temp_symlink_cannot_be_overwritten(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("safe")
    (pygame / ".gamelist.xml.tmp-dead").symlink_to(victim)
    monkeypatch.setattr(installer.secrets, "token_hex", lambda _size: "dead")

    with pytest.raises(FileExistsError):
        install(tmp_path, source_root, "one", "stable")

    assert victim.read_text() == "safe"
    assert not app(tmp_path).exists()


def test_forged_manifest_cannot_delete_arbitrary_in_root_file(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    victim = tmp_path / "keep-me"
    victim.write_text("safe")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["releases"] = ["../../keep-me"]
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert victim.read_text() == "safe"
    assert app(tmp_path).is_dir()


def test_gamelist_inode_change_before_replace_aborts(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    gamelist = tmp_path / "roms/pygame/gamelist.xml"
    gamelist.write_text("<gameList />")
    original = installer._replace_gamelist

    def race(directory_fd: int, info: os.stat_result | None, data: bytes, mode: int) -> None:
        gamelist.unlink()
        gamelist.write_text("<gameList><game><name>attacker</name></game></gameList>")
        original(directory_fd, info, data, mode)

    monkeypatch.setattr(installer, "_replace_gamelist", race)
    with pytest.raises(RuntimeError, match="changed during operation"):
        install(tmp_path, source_root, "two", "stable")
    assert "attacker" in gamelist.read_text()
    assert current(tmp_path)["release_id"] == "one"


def test_static_file_swap_to_symlink_during_validation_aborts(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = app(tmp_path) / "control-freak.pygame"
    victim = tmp_path / "same-content"
    victim.write_bytes(launcher.read_bytes())
    real = installer.checksum
    swapped = False

    def race(path: Path) -> str:
        nonlocal swapped
        if path == launcher and not swapped:
            swapped = True
            launcher.unlink()
            launcher.symlink_to(victim)
        return real(path)

    monkeypatch.setattr(installer, "checksum", race)
    with pytest.raises(RuntimeError, match="Modified stable file"):
        install(tmp_path, source_root, "two", "stable")
    assert launcher.is_symlink()
    assert current(tmp_path)["release_id"] == "one"


def test_unknown_release_and_unknown_root_object_are_preserved(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    unknown_release = app(tmp_path) / "releases/mystery"
    unknown_release.mkdir()
    (unknown_release / "payload").write_text("mine")

    with pytest.raises(RuntimeError, match="Unknown release"):
        install(tmp_path, source_root, "two", "stable")
    assert (unknown_release / "payload").read_text() == "mine"

    shutil.rmtree(unknown_release)
    unknown = app(tmp_path) / "notes"
    unknown.write_text("mine")
    with pytest.raises(RuntimeError, match="Unknown object"):
        uninstall(tmp_path)
    assert unknown.read_text() == "mine"


def test_update_recovers_after_release_publish_crash(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.ROOT_MANIFEST:
            raise KeyboardInterrupt
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(KeyboardInterrupt):
        install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path)["release_id"] == "one"

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path)["release_id"] == "two"


def test_update_recovers_after_manifest_switch_crash(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    gamelist = tmp_path / "roms/pygame/gamelist.xml"
    registration = gamelist.read_bytes()
    registration_inode = gamelist.stat().st_ino
    real = installer._atomic_write_at

    def crash(directory_fd: int, name: str, data: bytes, mode: int) -> None:
        if name == installer.CURRENT:
            raise SystemExit(7)
        real(directory_fd, name, data, mode)

    monkeypatch.setattr(installer, "_atomic_write_at", crash)
    with pytest.raises(SystemExit):
        install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path)["release_id"] == "one"
    assert gamelist.read_bytes() == registration
    assert gamelist.stat().st_ino == registration_inode

    monkeypatch.setattr(installer, "_atomic_write_at", real)
    install(tmp_path, source_root, "two", "nightly")
    assert current(tmp_path) == {"channel": "nightly", "release_id": "two", "version": 1}


def test_same_release_repairs_gamelist_without_rewriting_release(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    release = app(tmp_path) / "releases/one"
    inode = release.stat().st_ino
    (tmp_path / "roms/pygame/gamelist.xml").write_text("<gameList />")

    assert install(tmp_path, source_root, "one", "stable") == ["repair registration for one"]
    assert registered(tmp_path)
    assert release.stat().st_ino == inode


@pytest.mark.parametrize("remove", [False, True], ids=["install", "uninstall"])
def test_gamelist_edits_preserve_comments_and_processing_instructions(
    tmp_path: Path, source_root: Path, remove: bool
) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_text(
        "<?xml version='1.0'?>\n"
        "<!--before root--><?before instruction?>"
        "<gameList><!--inside root--><?inside instruction?></gameList>"
        "<!--after root--><?after instruction?>\n"
    )

    install(tmp_path, source_root, "one", "stable")
    if remove:
        uninstall(tmp_path)

    edited = gamelist.read_text()
    assert "<!--before root-->" in edited
    assert "<?before instruction?>" in edited
    assert "<!--inside root-->" in edited
    assert "<?inside instruction?>" in edited
    assert "<!--after root-->" in edited
    assert "<?after instruction?>" in edited


def test_gamelist_non_utf8_input_is_normalized_without_corruption(tmp_path: Path, source_root: Path) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_bytes(
        b'<?xml version="1.0" encoding="iso-8859-1"?>\n'
        b'<!--avant--><?avant instruction?>'
        b'<gameList><game><path>./other</path><name>Caf\xe9</name></game></gameList>'
        b'<!--apres--><?apres instruction?>\n'
    )

    install(tmp_path, source_root, "one", "stable")
    installed = gamelist.read_bytes()
    assert b'encoding="utf-8"' in installed
    assert "Café" in installed.decode("utf-8")
    assert "<!--avant-->" in installed.decode("utf-8")
    assert "<?apres instruction?>" in installed.decode("utf-8")

    uninstall(tmp_path)
    removed = gamelist.read_bytes()
    root = ET.fromstring(removed)
    assert root.findtext("game/name") == "Café"
    assert "<!--apres-->" in removed.decode("utf-8")


def test_install_rejects_non_gamelist_xml_root(tmp_path: Path, source_root: Path) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_text("<unexpected><keep>mine</keep></unexpected>")

    with pytest.raises(RuntimeError, match="Unexpected gamelist.xml root"):
        install(tmp_path, source_root, "one", "stable")

    assert gamelist.read_text() == "<unexpected><keep>mine</keep></unexpected>"
    assert not app(tmp_path).exists()


def test_concurrent_updates_are_serialized(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer._source_manifest
    first_entered = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    errors: list[BaseException] = []

    def controlled(source: Path, release_id: str, parent: Path) -> tuple[Path, dict]:
        nonlocal calls
        with calls_lock:
            calls += 1
            ordinal = calls
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(5)
        return real(source, release_id, parent)

    def update(release_id: str, done: threading.Event | None = None) -> None:
        try:
            install(tmp_path, source_root, release_id, "nightly")
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done:
                done.set()

    monkeypatch.setattr(installer, "_source_manifest", controlled)
    first = threading.Thread(target=update, args=("two",))
    second = threading.Thread(target=update, args=("three", second_done))
    first.start()
    assert first_entered.wait(5)
    second.start()
    assert not second_done.wait(0.1)
    with calls_lock:
        assert calls == 1
    release_first.set()
    first.join(5)
    second.join(5)

    assert errors == []
    assert not first.is_alive() and not second.is_alive()
    manifest = json.loads((app(tmp_path) / "install-manifest.json").read_text())
    assert manifest["releases"] == ["one", "two", "three"]
    assert current(tmp_path)["release_id"] == "three"


def test_force_reselects_same_release_only_when_source_matches(tmp_path: Path, source_root: Path) -> None:
    source = copied_source(tmp_path, source_root)
    install(tmp_path / "root", source, "one", "stable")
    release = app(tmp_path / "root") / "releases/one"
    inode = release.stat().st_ino

    operations = install(tmp_path / "root", source, "one", "nightly", force=True)
    assert operations[0] == "reselect release one"
    assert release.stat().st_ino == inode
    assert current(tmp_path / "root")["channel"] == "nightly"

    (source / "src/control_freak/app.py").write_text("changed source")
    with pytest.raises(RuntimeError, match="Source differs"):
        install(tmp_path / "root", source, "one", "stable", force=True)


def test_force_does_not_overwrite_unknown_current_pointer(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    pointer.write_text(json.dumps({"version": 1, "release_id": "unknown", "channel": "stable"}))

    with pytest.raises(RuntimeError, match="Invalid current pointer"):
        install(tmp_path, source_root, "one", "nightly", force=True)
    assert json.loads(pointer.read_text())["release_id"] == "unknown"


def test_current_pointer_symlink_is_unknown_and_preserved(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    contents = pointer.read_bytes()
    pointer.unlink()
    unknown = tmp_path / "unknown-current"
    unknown.write_bytes(contents)
    pointer.symlink_to(unknown)

    with pytest.raises(RuntimeError, match="current pointer"):
        install(tmp_path, source_root, "one", "nightly", force=True)
    assert pointer.is_symlink()
    assert unknown.read_bytes() == contents


@pytest.mark.parametrize(
    "relative",
    [
        "deploy/control-freak.pygame",
        "assets/control-freak-image.png",
        "assets/control-freak-logo-1.png",
        "assets/control-freak-logo-2.png",
        "assets/control-freak-logo-3.png",
        "deploy/install_batocera.py",
    ],
)
def test_update_refuses_changed_lifecycle_protocol_source(tmp_path: Path, source_root: Path, relative: str) -> None:
    source = copied_source(tmp_path, source_root)
    root = tmp_path / "root"
    install(root, source, "one", "stable")
    changed = source / relative
    changed.write_bytes(changed.read_bytes() + (b"\xff" if relative.endswith(".pygame") else b"\nchanged protocol\n"))

    with pytest.raises(RuntimeError, match="Lifecycle protocol mismatch"):
        install(root, source, "two", "nightly")

    assert current(root) == {"channel": "stable", "release_id": "one", "version": 1}
    assert not (app(root) / "releases/two").exists()
    assert registered(root)


def test_update_refuses_changed_stable_file_mode(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher = app(tmp_path) / "control-freak.pygame"
    launcher.chmod(0o644)

    with pytest.raises(RuntimeError, match="Modified stable file: control-freak.pygame"):
        install(tmp_path, source_root, "two", "nightly")

    assert current(tmp_path) == {"channel": "stable", "release_id": "one", "version": 1}
    assert launcher.stat().st_mode & 0o777 == 0o644
    assert not (app(tmp_path) / "releases/two").exists()


def test_upgrade_tracks_old_releases_and_uninstall_removes_all(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    launcher_hash = installer.checksum(app(tmp_path) / "control-freak.pygame")
    install(tmp_path, source_root, "two", "nightly")

    manifest = json.loads((app(tmp_path) / "install-manifest.json").read_text())
    assert manifest["releases"] == ["one", "two"]
    assert (app(tmp_path) / "releases/one").is_dir()
    assert installer.checksum(app(tmp_path) / "control-freak.pygame") == launcher_hash
    assert uninstall(tmp_path) == []
    assert not app(tmp_path).exists()
    assert not registered(tmp_path)


def test_uninstall_retries_after_unregister_boundary(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pygame = tmp_path / "roms/pygame"
    pygame.mkdir(parents=True)
    gamelist = pygame / "gamelist.xml"
    gamelist.write_text("<gameList><game><path>./other</path><name>Other</name></game></gameList>")
    install(tmp_path, source_root, "one", "stable")
    real = installer.os.rename

    def interrupt(source: str, destination: str, **kwargs: int) -> None:
        if source == "control-freak":
            raise SystemExit(9)
        real(source, destination, **kwargs)

    monkeypatch.setattr(installer.os, "rename", interrupt)
    with pytest.raises(SystemExit):
        uninstall(tmp_path)
    assert app(tmp_path).is_dir()
    assert not registered(tmp_path)

    monkeypatch.setattr(installer.os, "rename", real)
    assert uninstall(tmp_path) == []
    games = ET.parse(gamelist).getroot().findall("game")
    assert [(game.findtext("path"), game.findtext("name")) for game in games] == [("./other", "Other")]
    assert not app(tmp_path).exists()


def test_uninstall_retries_after_quarantine_rename(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(tmp_path, source_root, "one", "stable")
    real = installer.shutil.rmtree

    def interrupt(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".control-freak-remove-"):
            raise SystemExit(9)
        real(path, *args, **kwargs)

    monkeypatch.setattr(installer.shutil, "rmtree", interrupt)
    with pytest.raises(SystemExit):
        uninstall(tmp_path)
    quarantine = list((tmp_path / "roms/pygame").glob(".control-freak-remove-*"))
    assert len(quarantine) == 1
    assert not registered(tmp_path)

    monkeypatch.setattr(installer.shutil, "rmtree", real)
    assert uninstall(tmp_path) == []
    assert not quarantine[0].exists()
    assert not registered(tmp_path)


def test_uninstall_drift_makes_zero_changes(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    changed = app(tmp_path) / "releases/one/control_freak/app.py"
    changed.write_text("local change")

    with pytest.raises(RuntimeError, match="Modified release file"):
        uninstall(tmp_path)

    assert changed.read_text() == "local change"
    assert (app(tmp_path) / "install-manifest.json").is_file()
    assert registered(tmp_path)


@pytest.mark.parametrize("operation", ["update", "uninstall"])
def test_release_validation_rejects_unmanifested_empty_directory_before_mutation(
    tmp_path: Path, source_root: Path, operation: str
) -> None:
    install(tmp_path, source_root, "one", "stable")
    unexpected = app(tmp_path) / "releases/one/unmanifested"
    unexpected.mkdir()
    gamelist = tmp_path / "roms/pygame/gamelist.xml"
    before = gamelist.read_bytes()

    with pytest.raises(RuntimeError, match="Unknown release objects"):
        if operation == "update":
            install(tmp_path, source_root, "two", "nightly")
        else:
            uninstall(tmp_path)

    assert unexpected.is_dir()
    assert current(tmp_path) == {"channel": "stable", "release_id": "one", "version": 1}
    assert gamelist.read_bytes() == before


def test_uninstall_conflicting_gamelist_makes_zero_changes(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    gamelist = tmp_path / "roms/pygame/gamelist.xml"
    gamelist.write_text(
        f"<gameList><game><path>{installer.GAME_PATH}</path><name>Someone else</name></game></gameList>"
    )

    with pytest.raises(RuntimeError, match="Refusing to replace an unknown gamelist entry"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert "Someone else" in gamelist.read_text()


def test_relative_root_has_working_pointer(tmp_path: Path, source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    install(Path("userdata"), source_root, "one", "stable")

    pointer = current(tmp_path / "userdata")
    release = app(tmp_path / "userdata") / "releases" / pointer["release_id"]
    assert release.is_dir()
    assert not (app(tmp_path / "userdata") / "current").exists()


def test_existing_root_through_symlinked_ancestor_is_refused(tmp_path: Path, source_root: Path) -> None:
    real = tmp_path / "real"
    (real / "userdata").mkdir(parents=True)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlinked userdata path"):
        install(link / "userdata", source_root, "one", "stable")

    assert not app(real / "userdata").exists()


def test_manifest_schema_requires_exact_types(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["version"] = True
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_current_schema_requires_exact_types(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    pointer = app(tmp_path) / "current.json"
    value = json.loads(pointer.read_text())
    value["version"] = True
    pointer.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid current pointer"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_manifest_rejects_non_string_release_without_type_error(tmp_path: Path, source_root: Path) -> None:
    install(tmp_path, source_root, "one", "stable")
    manifest = app(tmp_path) / "install-manifest.json"
    value = json.loads(manifest.read_text())
    value["releases"] = [{}]
    manifest.write_text(json.dumps(value))

    with pytest.raises(RuntimeError, match="Invalid install manifest"):
        uninstall(tmp_path)

    assert app(tmp_path).is_dir()
    assert registered(tmp_path)


def test_install_refuses_unknown_launcher_even_with_force(tmp_path: Path, source_root: Path) -> None:
    launcher = app(tmp_path) / "control-freak.pygame"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("unknown local launcher\n")
    with pytest.raises(RuntimeError, match="unknown application root"):
        install(tmp_path, source_root, "one", "stable", force=True)


def test_install_dry_run_does_not_write(tmp_path: Path, source_root: Path) -> None:
    operations = install(tmp_path, source_root, "one", "stable", dry_run=True)
    assert operations[0] == "install release one"
    assert list(tmp_path.iterdir()) == []
