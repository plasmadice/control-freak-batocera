from __future__ import annotations

import pygame

from .model import Action


KEY_ACTIONS = {
    pygame.K_UP: Action.UP,
    pygame.K_w: Action.UP,
    pygame.K_DOWN: Action.DOWN,
    pygame.K_s: Action.DOWN,
    pygame.K_LEFT: Action.LEFT,
    pygame.K_a: Action.LEFT,
    pygame.K_RIGHT: Action.RIGHT,
    pygame.K_d: Action.RIGHT,
    pygame.K_RETURN: Action.SELECT,
    pygame.K_SPACE: Action.SELECT,
    pygame.K_ESCAPE: Action.BACK,
    pygame.K_BACKSPACE: Action.BACK,
}


def actions_for_event(event: pygame.event.Event) -> list[Action]:
    if event.type == pygame.KEYDOWN and event.key in KEY_ACTIONS:
        return [KEY_ACTIONS[event.key]]
    if event.type == pygame.JOYHATMOTION:
        x, y = event.value
        if y:
            return [Action.UP if y > 0 else Action.DOWN]
        if x:
            return [Action.RIGHT if x > 0 else Action.LEFT]
    if event.type == pygame.JOYBUTTONDOWN:
        if event.button == 0:
            return [Action.SELECT]
        if event.button in (1, 6):
            return [Action.BACK]
    return []
