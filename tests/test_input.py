import pygame

from control_freak.input import EventRouter, actions_for_event, es_controller_mappings
from control_freak.model import Action


def test_keyboard_maps_to_semantic_actions() -> None:
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_UP)) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == [Action.SELECT]
    assert actions_for_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)) == [Action.BACK]


def test_sdl_controller_uses_physical_face_positions() -> None:
    assert actions_for_event(
        pygame.event.Event(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_A)
    ) == [Action.SELECT]
    assert actions_for_event(
        pygame.event.Event(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_B)
    ) == [Action.BACK]


def test_start_and_select_never_activate_a_row() -> None:
    assert actions_for_event(
        pygame.event.Event(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_START)
    ) == []
    assert actions_for_event(
        pygame.event.Event(pygame.CONTROLLERBUTTONDOWN, button=pygame.CONTROLLER_BUTTON_BACK)
    ) == []


def test_emulationstation_mapping_is_translated_to_sdl_positions(tmp_path) -> None:
    config = tmp_path / "es_input.cfg"
    config.write_text(
        """<inputList><inputConfig type="joystick" deviceName="PLAYSTATION(R)3 Controller" deviceGUID="050000004c0500006802000000800000">
        <input name="a" type="button" id="13" value="1" />
        <input name="b" type="button" id="14" value="1" />
        <input name="start" type="button" id="3" value="1" />
        <input name="select" type="button" id="0" value="1" />
        <input name="up" type="button" id="4" value="1" />
        </inputConfig></inputList>"""
    )

    mappings = es_controller_mappings(config, ["PLAYSTATION(R)3 Controller"])

    assert len(mappings) == 1
    assert "a:b14" in mappings[0]
    assert "b:b13" in mappings[0]
    assert "start:b3" in mappings[0]
    assert "back:b0" in mappings[0]
    assert "dpup:b4" in mappings[0]


def test_shared_select_hotkey_is_not_mapped_twice(tmp_path) -> None:
    config = tmp_path / "es_input.cfg"
    config.write_text(
        """<inputList><inputConfig type="joystick" deviceName="Pad" deviceGUID="050000004c0500006802000000800000">
        <input name="select" type="button" id="8" value="1" />
        <input name="hotkey" type="button" id="8" value="1" />
        </inputConfig></inputList>"""
    )

    mapping = es_controller_mappings(config, ["Pad"])[0]
    assert "back:b8" in mapping
    assert "guide:b8" not in mapping


def test_axis_hat_and_connected_name_selection(tmp_path) -> None:
    config = tmp_path / "es_input.cfg"
    config.write_text(
        """<inputList>
        <inputConfig type="joystick" deviceName="Wrong" deviceGUID="050000004c0500006802000000800000">
          <input name="b" type="button" id="9" value="1" />
        </inputConfig>
        <inputConfig type="joystick" deviceName="Connected" deviceGUID="050000004c0500006802000000800000">
          <input name="b" type="button" id="0" value="1" />
          <input name="up" type="hat" id="0" value="1" />
          <input name="joystick1left" type="axis" id="0" value="-1" />
          <input name="l2" type="axis" id="2" value="1" />
        </inputConfig>
        </inputList>"""
    )

    mapping = es_controller_mappings(config, ["Connected"])[0]
    assert ",Connected," in mapping
    assert "a:b0" in mapping and "a:b9" not in mapping
    assert "dpup:h0.1" in mapping
    assert "leftx:a0" in mapping
    assert "lefttrigger:a2" in mapping


def test_mapped_controller_ignores_duplicate_raw_joystick_events() -> None:
    event = pygame.event.Event(pygame.JOYBUTTONDOWN, instance_id=7, button=0)
    assert actions_for_event(event, {7}) == []
    assert actions_for_event(event, set()) == [Action.SELECT]


def test_pad_event_suppresses_batocera_keyboard_echo() -> None:
    router = EventRouter({7})

    actions = router.actions(
        [
            pygame.event.Event(
                pygame.CONTROLLERBUTTONDOWN,
                instance_id=7,
                button=pygame.CONTROLLER_BUTTON_START,
            ),
            pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN),
        ],
        now=10.0,
    )
    assert actions == []
    assert router.actions([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)], now=10.5) == []
    assert router.actions([], now=10.57) == [Action.SELECT]


def test_keyboard_echo_is_suppressed_when_it_arrives_before_pad_event() -> None:
    router = EventRouter({7})

    assert router.actions([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)], now=10.0) == []
    assert router.actions(
        [pygame.event.Event(pygame.CONTROLLERBUTTONDOWN, instance_id=7, button=pygame.CONTROLLER_BUTTON_START)],
        now=10.01,
    ) == []
    assert router.actions([], now=10.1) == []


def test_diagonal_hat_emits_one_action_with_vertical_priority() -> None:
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(1, 1))) == [Action.UP]
    assert actions_for_event(pygame.event.Event(pygame.JOYHATMOTION, value=(-1, -1))) == [Action.DOWN]
