import pygame

from control_freak.input import ControllerIdentity, EventRouter, actions_for_event, load_controller_bindings
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

    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=0), bindings) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=1), bindings) == [Action.BACK]
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=13), bindings) == [Action.UP]


def test_start_and_select_never_activate_a_row(tmp_path) -> None:
    config = tmp_path / "es_input.cfg"
    config.write_text(
        """<inputList><inputConfig type="joystick" deviceName="Pad" deviceGUID="guid-one">
        <input name="b" type="button" id="0" value="1" />
        <input name="a" type="button" id="1" value="1" />
        <input name="start" type="button" id="3" value="1" />
        <input name="select" type="button" id="8" value="1" />
        </inputConfig></inputList>"""
    )
    bindings = load_controller_bindings(config, [ControllerIdentity(7, "Pad", "guid-one")])

    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=3), bindings) == []
    assert actions_for_event(pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=8), bindings) == []


def test_pad_event_suppresses_batocera_keyboard_echo() -> None:
    bindings = {7: {3: None}}
    router = EventRouter(bindings)

    actions = router.actions(
        [
            pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=3),
            pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN),
        ],
        now=10.0,
    )
    assert actions == []
    assert router.actions([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)], now=10.5) == [Action.SELECT]


def test_diagonal_hat_emits_one_action_with_vertical_priority() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 1))) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(-1, -1))) == [Action.DOWN]
