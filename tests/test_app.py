import pygame

from control_freak.app import _display_mode, _local_ipv4


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


def test_fullscreen_uses_desktop_dimensions() -> None:
    size, flags = _display_mode(False, [(1920, 1080)])

    assert size == (1920, 1080)
    assert flags == pygame.FULLSCREEN


def test_windowed_uses_logical_canvas_dimensions() -> None:
    size, flags = _display_mode(True, [(3840, 2160)])

    assert size == (1280, 720)
    assert flags == pygame.RESIZABLE
