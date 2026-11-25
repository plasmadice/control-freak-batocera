from __future__ import annotations

import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import pygame
from pygame._sdl2 import controller as sdlcontroller

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

# EmulationStation uses Nintendo face names; SDL uses physical positions.
ES_TO_SDL = {
    "b": "a",
    "a": "b",
    "y": "x",
    "x": "y",
    "up": "dpup",
    "down": "dpdown",
    "left": "dpleft",
    "right": "dpright",
    "start": "start",
    "select": "back",
    "hotkey": "guide",
    "pageup": "leftshoulder",
    "pagedown": "rightshoulder",
    "l2": "lefttrigger",
    "r2": "righttrigger",
    "l3": "leftstick",
    "r3": "rightstick",
    "joystick1left": "leftx",
    "joystick1up": "lefty",
    "joystick2left": "rightx",
    "joystick2up": "righty",
}


def _sdl_binding(item: ET.Element) -> str | None:
    name = item.get("name", "")
    target = ES_TO_SDL.get(name)
    kind = item.get("type")
    number = item.get("id", "")
    if target is None or not number.isdigit():
        return None
    try:
        value = int(item.get("value", "0"))
    except ValueError:
        return None
    if kind == "button":
        return f"{target}:b{number}"
    if kind == "hat":
        return f"{target}:h{number}.{value}"
    if kind != "axis":
        return None
    if name.startswith("joystick"):
        return f"{target}:a{number}{'~' if value > 0 else ''}"
    if target.startswith("dp"):
        return f"{target}:{'-' if value < 0 else '+'}a{number}"
    return f"{target}:a{number}{'~' if value < 0 else ''}"


def es_controller_mappings(path: Path, connected_names: list[str]) -> list[str]:
    try:
        configs = ET.parse(path).getroot().findall("inputConfig")
    except (OSError, ET.ParseError):
        return []

    chosen: dict[str, tuple[str, list[str]]] = {}
    for config in configs:
        guid = (config.get("deviceGUID") or "").lower()
        name = config.get("deviceName") or "Controller"
        if config.get("type") != "joystick" or len(guid) != 32:
            continue
        if guid in chosen and (chosen[guid][0] in connected_names or name not in connected_names):
            continue
        bindings = []
        button_ids = set()
        hotkey: tuple[str, str | None, str] | None = None
        for item in config.findall("input"):
            binding = _sdl_binding(item)
            if not binding:
                continue
            kind, number = item.get("type"), item.get("id", "")
            if item.get("name") == "hotkey":
                hotkey = (binding, kind, number)
                continue
            bindings.append(binding)
            if kind == "button":
                button_ids.add(number)
        if hotkey and not (hotkey[1] == "button" and hotkey[2] in button_ids):
            bindings.append(hotkey[0])
        if bindings:
            chosen[guid] = (name, bindings)

    return [
        ",".join((guid, name.replace(",", "."), "platform:Linux", *sorted(set(bindings)))) + ","
        for guid, (name, bindings) in chosen.items()
    ]


def configure_sdl_controllers(path: Path) -> tuple[list[object], set[int]]:
    pygame.joystick.init()
    names = [pygame.joystick.Joystick(index).get_name() for index in range(pygame.joystick.get_count())]
    mappings = es_controller_mappings(path, names)
    if mappings:
        existing = os.environ.get("SDL_GAMECONTROLLERCONFIG")
        os.environ["SDL_GAMECONTROLLERCONFIG"] = "\n".join(mappings + ([existing] if existing else []))
        if sdlcontroller.get_init():
            sdlcontroller.quit()
        pygame.joystick.quit()
        pygame.joystick.init()
    sdlcontroller.init()

    handles: list[object] = []
    mapped_ids: set[int] = set()
    for index in range(pygame.joystick.get_count()):
        if not sdlcontroller.is_controller(index):
            continue
        handles.append(sdlcontroller.Controller(index))
        mapped_ids.add(pygame.joystick.Joystick(index).get_instance_id())
    return handles, mapped_ids


def actions_for_event(event: pygame.event.Event, mapped_ids: set[int] | None = None) -> list[Action]:
    if event.type == pygame.KEYDOWN and event.key in KEY_ACTIONS:
        return [KEY_ACTIONS[event.key]]
    if event.type == pygame.CONTROLLERBUTTONDOWN:
        return {
            pygame.CONTROLLER_BUTTON_A: [Action.SELECT],
            pygame.CONTROLLER_BUTTON_B: [Action.BACK],
            pygame.CONTROLLER_BUTTON_DPAD_UP: [Action.UP],
            pygame.CONTROLLER_BUTTON_DPAD_DOWN: [Action.DOWN],
            pygame.CONTROLLER_BUTTON_DPAD_LEFT: [Action.LEFT],
            pygame.CONTROLLER_BUTTON_DPAD_RIGHT: [Action.RIGHT],
        }.get(event.button, [])
    if event.type == pygame.JOYHATMOTION:
        if mapped_ids and getattr(event, "instance_id", None) in mapped_ids:
            return []
        x, y = event.value
        if y:
            return [Action.UP if y > 0 else Action.DOWN]
        if x:
            return [Action.RIGHT if x > 0 else Action.LEFT]
    if event.type == pygame.JOYBUTTONDOWN:
        instance_id = getattr(event, "instance_id", getattr(event, "joy", None))
        if mapped_ids and instance_id in mapped_ids:
            return []
        if event.button == 0:
            return [Action.SELECT]
        if event.button == 1:
            return [Action.BACK]
    return []


class EventRouter:
    """Route SDL controller events and suppress Batocera's keyboard echoes."""

    def __init__(self, mapped_ids: set[int] | None = None, controller_handles: list[object] | None = None) -> None:
        self.mapped_ids = mapped_ids or set()
        self.controller_handles = controller_handles or []
        self.last_pad_activity = float("-inf")
        self.pending_keys: list[tuple[float, Action]] = []

    def actions(self, events: list[pygame.event.Event], now: float | None = None) -> list[Action]:
        timestamp = time.monotonic() if now is None else now
        pad_types = {
            pygame.CONTROLLERBUTTONDOWN,
            pygame.CONTROLLERBUTTONUP,
            pygame.JOYBUTTONDOWN,
            pygame.JOYBUTTONUP,
            pygame.JOYHATMOTION,
        }
        if any(event.type in pad_types for event in events):
            self.last_pad_activity = timestamp
        output = []
        for event in events:
            if event.type == pygame.KEYDOWN and event.key in KEY_ACTIONS:
                self.pending_keys.append((timestamp, KEY_ACTIONS[event.key]))
                continue
            output.extend(actions_for_event(event, self.mapped_ids))
        pending = []
        for queued_at, action in self.pending_keys:
            if abs(self.last_pad_activity - queued_at) < 0.3:
                continue
            if timestamp - queued_at >= 0.06:
                output.append(action)
            else:
                pending.append((queued_at, action))
        self.pending_keys = pending
        return output
