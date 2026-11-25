import pygame

from control_freak.input import actions_for_event
from control_freak.model import Action


def test_keyboard_maps_to_semantic_actions() -> None:
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_UP)) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)) == [Action.BACK]


def test_controller_hat_and_standard_buttons_map_to_actions() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 0))) == [Action.RIGHT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0)) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=1)) == [Action.BACK]


def test_diagonal_hat_emits_one_action_with_vertical_priority() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 1))) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(-1, -1))) == [Action.DOWN]
