import pygame

from control_freak.input import ControllerIdentity, actions_for_event, load_controller_bindings
from control_freak.model import Action


def test_keyboard_maps_to_semantic_actions() -> None:
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_UP)) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)) == [Action.BACK]


def test_controller_hat_and_standard_buttons_map_to_actions() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 0))) == [Action.RIGHT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0)) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=1)) == [Action.BACK]


def test_uses_emulationstation_button_mapping_for_connected_controller(tmp_path) -> None:
    config = tmp_path / "es_input.cfg"
    config.write_text(
        """<inputList><inputConfig type="joystick" deviceName="Sony PLAYSTATION(R)3 Controller" deviceGUID="guid-one">
        <input name="a" type="button" id="1" value="1" />
        <input name="b" type="button" id="0" value="1" />
        <input name="up" type="button" id="13" value="1" />
        </inputConfig></inputList>"""
    )
    bindings = load_controller_bindings(
        config,
        [ControllerIdentity(instance_id=7, name="Sony PLAYSTATION(R)3 Controller", guid="guid-one")],
    )

    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=0), bindings) == [Action.BACK]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=1), bindings) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=13), bindings) == [Action.UP]


def test_diagonal_hat_emits_one_action_with_vertical_priority() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 1))) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(-1, -1))) == [Action.DOWN]
