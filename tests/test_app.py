import pygame
from types import SimpleNamespace

import control_freak.app as app
from control_freak.app import _connection_type, _display_mode, _installed_version, _local_ipv4, _tailscale_ipv4


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


def test_connection_type_distinguishes_wireless_and_ethernet(tmp_path) -> None:
    wireless = tmp_path / "wlan0/wireless"
    wireless.mkdir(parents=True)
    routes = "Iface\tDestination\nlo\t00000000\nwlan0\t00000000\n"
    assert _connection_type(routes, tmp_path) == "Wireless"

    routes = "Iface\tDestination\neth0\t00000000\n"
    assert _connection_type(routes, tmp_path) == "Ethernet"


def test_tailscale_address_requires_cli_and_connected_ip(monkeypatch) -> None:
    monkeypatch.setattr(app.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(app.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="100.64.0.50\n"))
    assert _tailscale_ipv4() == "100.64.0.50"

    monkeypatch.setattr(app.shutil, "which", lambda _name: None)
    assert _tailscale_ipv4() is None


def test_tailscale_address_rejects_timeout_and_invalid_output(monkeypatch) -> None:
    monkeypatch.setattr(app.shutil, "which", lambda _name: "/usr/bin/tailscale")
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
