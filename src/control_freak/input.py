from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

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

ES_BUTTON_ACTIONS = {
    "up": Action.UP,
    "down": Action.DOWN,
    "left": Action.LEFT,
    "right": Action.RIGHT,
    "a": Action.SELECT,
    "b": Action.BACK,
}


@dataclass(frozen=True)
class ControllerIdentity:
    instance_id: int
    name: str
    guid: str


def load_controller_bindings(path: Path, controllers: list[ControllerIdentity]) -> dict[int, dict[int, Action]]:
    try:
        configs = ET.parse(path).getroot().findall("inputConfig")
    except (OSError, ET.ParseError):
        return {}

    bindings: dict[int, dict[int, Action]] = {}
    for controller in controllers:
        matches = [
            config
            for config in configs
            if config.get("deviceGUID") == controller.guid and config.get("deviceName") == controller.name
        ]
        if not matches:
            matches = [config for config in configs if config.get("deviceGUID") == controller.guid]
        if not matches:
            matches = [config for config in configs if config.get("deviceName") == controller.name]
        if not matches:
            continue
        buttons = {
            int(item.get("id", "-1")): ES_BUTTON_ACTIONS[item.get("name", "")]
            for item in matches[-1].findall("input")
            if item.get("type") == "button" and item.get("name") in ES_BUTTON_ACTIONS
        }
        bindings[controller.instance_id] = buttons
    return bindings


def actions_for_event(
    event: pygame.event.Event,
    controller_bindings: dict[int, dict[int, Action]] | None = None,
) -> list[Action]:
    if event.type == pygame.KEYDOWN and event.key in KEY_ACTIONS:
        return [KEY_ACTIONS[event.key]]
    if event.type == pygame.JOYHATMOTION:
        x, y = event.value
        if y:
            return [Action.UP if y > 0 else Action.DOWN]
        if x:
            return [Action.RIGHT if x > 0 else Action.LEFT]
    if event.type == pygame.JOYBUTTONDOWN:
        instance_id = getattr(event, "instance_id", getattr(event, "joy", None))
        if controller_bindings is not None and instance_id in controller_bindings:
            action = controller_bindings[instance_id].get(event.button)
            return [action] if action else []
        if event.button == 0:
            return [Action.SELECT]
        if event.button in (1, 6):
            return [Action.BACK]
    return []
