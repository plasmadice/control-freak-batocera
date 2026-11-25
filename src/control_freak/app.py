from __future__ import annotations

import argparse
import os
from pathlib import Path

import pygame

from .input import actions_for_event
from .model import build_fixture_model
from .ui import HEIGHT, WIDTH, clear_font_cache, render


def _init_joysticks() -> None:
    pygame.joystick.init()
    for index in range(pygame.joystick.get_count()):
        pygame.joystick.Joystick(index).init()


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


def run(windowed: bool = False) -> None:
    pygame.init()
    _init_joysticks()
    flags = pygame.RESIZABLE if windowed else pygame.FULLSCREEN
    screen = pygame.display.set_mode((WIDTH, HEIGHT) if windowed else (0, 0), flags)
    pygame.display.set_caption("Control Freak")
    canvas = pygame.Surface((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    model = build_fixture_model()

    while model.running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                model.running = False
            for action in actions_for_event(event):
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
