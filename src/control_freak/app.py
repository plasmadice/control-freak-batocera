from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket

import pygame

from .input import ControllerIdentity, actions_for_event, load_controller_bindings
from .model import build_fixture_model
from .ui import HEIGHT, WIDTH, clear_font_cache, render


ES_INPUT_CONFIG = Path("/userdata/system/configs/emulationstation/es_input.cfg")


def _init_joysticks() -> list[ControllerIdentity]:
    pygame.joystick.init()
    controllers = []
    for index in range(pygame.joystick.get_count()):
        joystick = pygame.joystick.Joystick(index)
        joystick.init()
        controllers.append(
            ControllerIdentity(joystick.get_instance_id(), joystick.get_name(), joystick.get_guid())
        )
    return controllers


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


def run(windowed: bool = False) -> None:
    pygame.init()
    controller_bindings = load_controller_bindings(ES_INPUT_CONFIG, _init_joysticks())
    size, flags = _display_mode(windowed, pygame.display.get_desktop_sizes())
    screen = pygame.display.set_mode(size, flags)
    pygame.display.set_caption("Control Freak")
    canvas = pygame.Surface((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    model = build_fixture_model(socket.gethostname().upper(), _local_ipv4())

    while model.running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                model.running = False
            for action in actions_for_event(event, controller_bindings):
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
