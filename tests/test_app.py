from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pygame

import control_freak.app as app
from control_freak.app import _apply_preferences_change, _apply_service_change, _apply_web_change, _check_update, _connection_type, _display_mode, _finish_update_check, _installed_version, _local_ipv4, _refresh_pairing, _show_release_notes, _start_update_check, _tailscale_ipv4
from control_freak.model import build_fixture_model


class FakeSocket:
    def __init__(self, address: str | None) -> None:
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def connect(self, _target) -> None:
        if self.address is None:
            raise OSError("offline")

    def getsockname(self):
        return self.address, 1234


def test_local_ipv4_reports_routed_address() -> None:
    assert _local_ipv4(lambda *_args: FakeSocket("192.168.1.50")) == "192.168.1.50"


def test_local_ipv4_reports_offline_without_crashing() -> None:
    assert _local_ipv4(lambda *_args: FakeSocket(None)) == "Offline"


def test_local_ipv4_rejects_an_invalid_address() -> None:
    assert _local_ipv4(lambda *_args: FakeSocket("not-an-ip")) == "Offline"


def test_connection_type_distinguishes_wireless_and_ethernet(tmp_path) -> None:
    wireless = tmp_path / "wlan0/wireless"
    wireless.mkdir(parents=True)
    routes = "Iface\tDestination\nlo\t00000000\nwlan0\t00000000\n"
    assert _connection_type(routes, tmp_path) == "Wireless"

    routes = "Iface\tDestination\neth0\t00000000\n"
    assert _connection_type(routes, tmp_path) == "Ethernet"


def test_tailscale_address_requires_cli_and_connected_ip(monkeypatch) -> None:
    monkeypatch.setattr(app, "TAILSCALE", Path(__file__))
    monkeypatch.setattr(app.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="100.64.0.50\n"))
    assert _tailscale_ipv4() == "100.64.0.50"

    monkeypatch.setattr(app, "TAILSCALE", Path("/definitely/missing/tailscale"))
    assert _tailscale_ipv4() is None


def test_tailscale_address_rejects_timeout_and_invalid_output(monkeypatch) -> None:
    monkeypatch.setattr(app, "TAILSCALE", Path(__file__))
    monkeypatch.setattr(app.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-an-ip\n"))
    assert _tailscale_ipv4() is None

    def timeout(*_args, **_kwargs):
        raise app.subprocess.TimeoutExpired("tailscale", 2)

    monkeypatch.setattr(app.subprocess, "run", timeout)
    assert _tailscale_ipv4() is None


def test_installed_version_reads_current_pointer(tmp_path) -> None:
    pointer = tmp_path / "current.json"
    pointer.write_text('{"release_id":"0.1.0-dev-test"}')
    assert _installed_version(pointer) == "0.1.0-dev-test"


def test_fullscreen_uses_desktop_dimensions() -> None:
    size, flags = _display_mode(False, [(1920, 1080)])

    assert size == (1920, 1080)
    assert flags == pygame.FULLSCREEN


def test_windowed_uses_logical_canvas_dimensions() -> None:
    size, flags = _display_mode(True, [(3840, 2160)])

    assert size == (1280, 720)
    assert flags == pygame.RESIZABLE


def test_web_configuration_failure_restores_model_and_reports_error() -> None:
    model = build_fixture_model(
        ip_address="192.168.1.50",
        tailscale_address="100.64.0.50",
        web_runtime_available=True,
        web_token="pair-token",
    )
    model.sync_web(True, True, True, "pair-token")

    def fail(_enabled: bool, _tailscale: bool) -> dict:
        raise OSError("read-only filesystem")

    assert _apply_web_change(model, (False, False), (True, True), fail) is False
    assert model.row("web-access").value is False
    assert model.row("web-address").available is False
    assert model.row("web-address").qr_value is None
    assert model.row("tailscale").available is False
    assert model.row("tailscale-address").qr_value is None
    assert model.current_row.description == "Could not update Web app: read-only filesystem"


def test_web_health_failure_is_visible_in_local_ui() -> None:
    model = build_fixture_model(ip_address="192.168.1.50")
    model.row("web-access").value = True

    def fail(_enabled: bool, _tailscale: bool) -> dict:
        raise RuntimeError("The Web app did not become healthy")

    assert _apply_web_change(model, (False, False), (True, False), fail) is False
    assert model.row("web-access").value is False
    assert model.row("web-address").available is False
    assert model.row("web-access").description == "Could not update Web app: The Web app did not become healthy"


def test_update_preferences_are_saved_from_the_ui() -> None:
    model = build_fixture_model(update_channel="nightly", check_updates_daily=False)
    saved = []

    assert _apply_preferences_change(model, lambda **value: saved.append(value)) is True
    assert saved == [{"update_channel": "nightly", "check_updates_daily": False}]


def test_update_preference_failure_is_visible_without_losing_choices() -> None:
    model = build_fixture_model(update_channel="nightly", check_updates_daily=False)

    def fail(**_value) -> None:
        raise OSError("read-only filesystem")

    assert _apply_preferences_change(model, fail) is False
    assert model.row("update-channel").value == "Nightly"
    assert model.row("check-updates-daily").value is False
    assert model.current_row.description == "Could not save update settings: read-only filesystem"


def test_batocera_service_toggle_is_applied_from_the_ui() -> None:
    model = build_fixture_model(service_enabled=False)
    model.row("batocera-service").value = True

    assert _apply_service_change(model, False, True, configure=lambda enabled: enabled) is True


def test_failed_batocera_service_toggle_restores_the_visible_state() -> None:
    model = build_fixture_model(service_enabled=True)
    model.row("batocera-service").value = False

    def fail(_enabled):
        raise RuntimeError("service refused")

    assert _apply_service_change(model, True, False, configure=fail) is False
    assert model.row("batocera-service").value is True
    assert model.row("batocera-service").description == "Could not update Batocera service: service refused"


def test_local_launch_does_not_reconfigure_persisted_web_policy() -> None:
    source = Path(app.__file__).read_text()
    assert 'configure_web(True, web_state["tailscale"])' not in source


def test_running_web_app_refreshes_a_local_only_pairing_credential() -> None:
    state = {"enabled": True, "tailscale": False}
    calls = []

    token = _refresh_pairing(Path("web.json"), state, lambda path, **settings: calls.append((path, settings)) or {"pairing_token": "raw-local-token"})

    assert token == "raw-local-token"
    assert calls == [(Path("web.json"), {"enabled": True, "tailscale": False})]


def test_manual_update_check_marks_candidate_and_requires_install_confirmation() -> None:
    model = build_fixture_model()
    candidate = {"available": True, "release_id": "1.2.3", "_candidate": {"signed": True}}

    assert _check_update(model, "1.0.0", b"key", check=lambda *_args: candidate) == {"signed": True}
    assert model.row("check-updates").value == "1.2.3 available"
    assert model.pending_confirmation is not None
    assert "Install verified 1.2.3" in model.pending_confirmation.message


def test_manual_update_check_immediately_shows_progress_without_blocking() -> None:
    model = build_fixture_model()
    release = Event()

    def wait_for_release(*_args):
        release.wait(1)
        return {"available": False}

    future = _start_update_check(model, "1.0.0", b"key", wait_for_release)

    assert future.done() is False
    assert model.row("check-updates").value == "Checking…"
    assert model.row("check-updates").description == "Checking the signed release feed…"
    assert model.row("check-updates").available is True
    release.set()
    assert _finish_update_check(model, future) is None
    assert model.row("check-updates").value == "Up to date"
    assert model.row("check-updates").available is True


def test_repeated_update_request_keeps_existing_progress() -> None:
    model = build_fixture_model()
    release = Event()
    pending = _start_update_check(model, "1.0.0", b"key", lambda *_args: release.wait(1) or {"available": False})

    assert app._request_update_check(model, pending, "1.0.0", b"key") is pending
    assert model.row("check-updates").value == "Checking…"
    assert model.row("check-updates").description == "Checking the signed release feed…"
    release.set()


def test_update_result_is_discarded_after_channel_changes() -> None:
    model = build_fixture_model(update_channel="stable")
    candidate = {"available": True, "release_id": "1.2.3", "_candidate": {"signed": True}}
    pending = _start_update_check(model, "1.0.0", b"key", lambda *_args: candidate)
    model.row("update-channel").value = "Nightly"

    assert _finish_update_check(model, pending) is None
    assert model.pending_confirmation is None
    assert model.row("check-updates").value == "Check again"
    assert "channel changed" in model.row("check-updates").description.lower()


def test_update_check_failure_is_safe_and_visible() -> None:
    model = build_fixture_model()

    def fail(*_args):
        raise app.UpdateError("offline")

    assert _check_update(model, "1.0.0", b"key", check=fail) is None
    assert model.pending_confirmation is None
    assert model.row("check-updates").value == "Check failed"
    assert "offline" in model.row("check-updates").description


def test_authenticated_release_notes_open_in_overlay() -> None:
    model = build_fixture_model(notes_available=True)

    assert _show_release_notes(model, "1.2.3", b"key", lambda *_args: "Signed notes") is True
    assert model.overlay_title == "What's new in 1.2.3"
    assert model.overlay_pages == [["Signed notes"]]
