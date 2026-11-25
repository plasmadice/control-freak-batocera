from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pygame

from .input import EventRouter, configure_sdl_controllers
from .model import build_fixture_model
from .ui import HEIGHT, WIDTH, clear_font_cache, render


ES_INPUT_CONFIG = Path("/userdata/system/configs/emulationstation/es_input.cfg")


def save_screenshot(path: Path) -> None:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    clear_font_cache()
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model()
    model.select("web-access")
    render(surface, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(surface, path)
    clear_font_cache()
    pygame.quit()


def _display_mode(windowed: bool, desktop_sizes: list[tuple[int, int]]) -> tuple[tuple[int, int], int]:
    if windowed:
        return (WIDTH, HEIGHT), pygame.RESIZABLE
    return desktop_sizes[0], pygame.FULLSCREEN


def _local_ipv4(socket_factory=socket.socket) -> str:
    try:
        with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            return str(connection.getsockname()[0])
    except OSError:
        return "Offline"


def _connection_type(route_text: str | None = None, interfaces: Path = Path("/sys/class/net")) -> str:
    if route_text is None:
        try:
            route_text = Path("/proc/net/route").read_text()
        except OSError:
            return "Network"
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000" and fields[0] != "lo":
            return "Wireless" if (interfaces / fields[0] / "wireless").is_dir() else "Ethernet"
    return "Network"


def _tailscale_ipv4() -> str | None:
    command = shutil.which("tailscale")
    if command is None:
        return None
    try:
        result = subprocess.run([command, "ip", "-4"], capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    candidate = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    try:
        return str(ipaddress.IPv4Address(candidate))
    except ipaddress.AddressValueError:
        return None


def _installed_version(path: Path = Path("/userdata/roms/pygame/control-freak/current.json")) -> str:
    try:
        value = json.loads(path.read_text()).get("release_id")
    except (OSError, json.JSONDecodeError):
        return "Unknown"
    return value if isinstance(value, str) else "Unknown"


def run(windowed: bool = False) -> None:
    pygame.init()
    controller_handles, mapped_ids = configure_sdl_controllers(ES_INPUT_CONFIG)
    router = EventRouter(mapped_ids, controller_handles)
    size, flags = _display_mode(windowed, pygame.display.get_desktop_sizes())
    screen = pygame.display.set_mode(size, flags)
    pygame.display.set_caption("Control Freak")
    canvas = pygame.Surface((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    ip_address = _local_ipv4()
    model = build_fixture_model(
        socket.gethostname().upper(),
        ip_address,
        _connection_type(),
        tailscale_address=_tailscale_ipv4(),
        installed_version=_installed_version(),
    )

    while model.running:
        events = pygame.event.get()
        for event in events:
            if event.type == pygame.QUIT:
                model.running = False
        for action in router.actions(events):
            model.dispatch(action)

        render(canvas, model)
        destination = screen.get_rect()
        scale = min(destination.w / WIDTH, destination.h / HEIGHT)
        size = (round(WIDTH * scale), round(HEIGHT * scale))
        scaled = pygame.transform.smoothscale(canvas, size)
        screen.fill((0, 0, 0))
        screen.blit(scaled, scaled.get_rect(center=destination.center))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Control Freak fixture-backed UI prototype")
    parser.add_argument("--windowed", action="store_true", help="run in a resizable window")
    parser.add_argument("--screenshot", type=Path, help="render a deterministic 1280x720 screenshot and exit")
    args = parser.parse_args()
    if args.screenshot:
        save_screenshot(args.screenshot)
    else:
        run(args.windowed)


if __name__ == "__main__":
    main()
