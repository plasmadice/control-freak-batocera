from control_freak.model import Action, AppModel, RowKind, build_fixture_model


def test_fixture_covers_first_prototype_states() -> None:
    model = build_fixture_model()
    rows = [row for section in model.sections for row in section.rows]

    assert {row.kind for row in rows} >= {
        RowKind.READONLY,
        RowKind.TOGGLE,
        RowKind.CHOICE,
        RowKind.ACTION,
    }
    assert any(row.available is False and row.reason for row in rows)

    assert any(row.id == "rollback" for row in rows)
    assert any(len(row.description) > 100 for row in rows)


def test_local_app_omits_library_and_lockout_prone_controls() -> None:
    model = build_fixture_model(device_name="BATO", ip_address="192.168.1.50", connection_type="Wireless")
    row_ids = {row.id for section in model.sections for row in section.rows}

    assert model.row("device").value == "BATO"
    assert model.row("network-status").value == "Wireless · 192.168.1.50"
    assert row_ids.isdisjoint({"rom-scan", "resolution", "restart-emulationstation", "reboot", "health", "storage", "audio-output", "volume", "diagnostics"})


def test_navigation_wraps_sections_and_clamps_rows() -> None:
    model = build_fixture_model()

    model.dispatch(Action.UP)
    assert model.section_index == len(model.sections) - 1

    model.dispatch(Action.RIGHT)
    for _ in range(20):
        model.dispatch(Action.DOWN)
    assert model.row_index == len(model.current_section.rows) - 1

    model.dispatch(Action.LEFT)
    assert model.row_index == 0


def test_audio_outputs_have_independent_block_toggles() -> None:
    model = build_fixture_model(
        audio_outputs=[
            {"value": "analog", "label": "Built-in Analog Stereo"},
            {"value": "hdmi", "label": "Digital Stereo (HDMI)"},
        ],
        blocked_audio=["analog"],
    )

    assert model.row("audio-block:analog").value is True
    assert model.row("audio-block:analog").label == "Block audio from Analog"
    assert model.row("audio-block:analog").description == "Built-in Analog Stereo"
    assert model.row("audio-block:analog").field_value == "Built-in Analog Stereo"
    assert model.row("audio-block:hdmi").value is False
    assert model.row("audio-block:hdmi").label == "Block audio from HDMI"
    assert model.row("audio-block:hdmi").description == "Digital Stereo (HDMI)"
    assert "kept muted" not in model.row("audio-block:analog").description.lower()


def test_duplicate_audio_output_types_are_numbered() -> None:
    model = build_fixture_model(
        audio_outputs=[
            {"value": "dp-a", "label": "GPU Digital Stereo (DisplayPort)"},
            {"value": "dp-b", "label": "Dock Digital Stereo (DisplayPort)"},
            {"value": "hdmi-2", "label": "GPU Digital Stereo (HDMI 2)"},
        ]
    )

    assert model.row("audio-block:dp-a").label == "Block audio from DisplayPort 1"
    assert model.row("audio-block:dp-b").label == "Block audio from DisplayPort 2"
    assert model.row("audio-block:hdmi-2").label == "Block audio from HDMI 2"


def test_mixed_audio_output_numbers_remain_unique() -> None:
    model = build_fixture_model(
        audio_outputs=[
            {"value": "hdmi-2", "label": "GPU Digital Stereo (HDMI 2)"},
            {"value": "hdmi", "label": "Dock Digital Stereo (HDMI)"},
            {"value": "hdmi-duplicate", "label": "Capture Digital Stereo (HDMI 2)"},
        ]
    )

    assert model.row("audio-block:hdmi-2").label == "Block audio from HDMI 2"
    assert model.row("audio-block:hdmi").label == "Block audio from HDMI 1"
    assert model.row("audio-block:hdmi-duplicate").label == "Block audio from HDMI 3"


def test_explicit_audio_output_number_is_reserved_regardless_of_order() -> None:
    model = build_fixture_model(
        audio_outputs=[
            {"value": "hdmi-a", "label": "Dock A Digital Stereo (HDMI)"},
            {"value": "hdmi-b", "label": "Dock B Digital Stereo (HDMI)"},
            {"value": "hdmi-2", "label": "GPU Digital Stereo (HDMI 2)"},
        ]
    )

    assert model.row("audio-block:hdmi-a").label == "Block audio from HDMI 1"
    assert model.row("audio-block:hdmi-b").label == "Block audio from HDMI 3"
    assert model.row("audio-block:hdmi-2").label == "Block audio from HDMI 2"


def test_unnumbered_audio_output_labels_are_stable_across_discovery_order() -> None:
    outputs = [
        {"value": "hdmi-a", "label": "Dock A Digital Stereo (HDMI)"},
        {"value": "hdmi-b", "label": "Dock B Digital Stereo (HDMI)"},
        {"value": "hdmi-2", "label": "GPU Digital Stereo (HDMI 2)"},
    ]

    forward = build_fixture_model(audio_outputs=outputs)
    reverse = build_fixture_model(audio_outputs=list(reversed(outputs)))

    expected = {"hdmi-a": "Block audio from HDMI 1", "hdmi-b": "Block audio from HDMI 3", "hdmi-2": "Block audio from HDMI 2"}
    for value, label in expected.items():
        assert forward.row(f"audio-block:{value}").label == label
        assert reverse.row(f"audio-block:{value}").label == label


def test_web_access_requires_confirmation_and_updates_dependents() -> None:
    model = build_fixture_model(ip_address="192.168.1.50", tailscale_installed=True, web_runtime_available=True, web_token="pair-token")
    model.select("web-access")

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None
    assert model.pending_confirmation.message.startswith("Enable")
    assert model.row("web-access").value is False

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is None
    assert model.row("web-access").value is True
    assert model.row("web-address").available is True
    assert model.row("web-address").value == "https://192.168.1.50:8787"
    assert model.row("web-address").qr_value == "https://192.168.1.50:8787/pair?token=pair-token"
    assert model.row("web-address").reason == ""
    assert model.row("tailscale").available is True
    assert model.row("tailscale").reason == ""


def test_web_request_does_not_claim_unavailable_service_is_running() -> None:
    model = build_fixture_model()
    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)

    assert model.row("web-access").value is True
    assert model.row("web-address").available is False
    assert model.row("web-address").value == "Unavailable"
    assert model.row("web-address").reason == "The Web app did not start."


def test_tailscale_requires_installation_valid_address_and_web_access() -> None:
    model = build_fixture_model(tailscale_installed=False)
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "Tailscale is not installed or connected on this machine."

    invalid = build_fixture_model(tailscale_address="not-an-ip", web_runtime_available=True, web_enabled=True)
    assert invalid.row("tailscale").available is False

    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "The Web app did not start."


def test_tailscale_address_stays_hidden_until_its_toggle_is_enabled() -> None:
    model = build_fixture_model(tailscale_address="100.64.0.50", web_runtime_available=True, web_token="pair-token")
    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)

    assert model.row("tailscale").available is True
    assert model.row("tailscale-address").available is False
    assert model.row("tailscale-address").value == "Hidden"

    model.select("tailscale")
    model.dispatch(Action.SELECT)
    assert model.row("tailscale-address").available is True
    assert model.row("tailscale-address").value == "https://100.64.0.50:8787"
    assert model.row("tailscale-address").qr_value == "https://100.64.0.50:8787/pair?token=pair-token"


def test_web_access_is_named_web_app() -> None:
    model = build_fixture_model()
    assert model.row("web-access").label == "Web app"
    assert "Web access" not in model.row("web-access").description


def test_running_web_app_restores_addresses_and_qr_codes() -> None:
    model = build_fixture_model(
        ip_address="192.168.1.50",
        tailscale_address="100.64.0.50",
        web_runtime_available=True,
        web_enabled=True,
        tailscale_enabled=True,
        web_token="pair-token",
    )

    assert model.row("web-access").value is True
    assert model.row("web-address").value == "https://192.168.1.50:8787"
    assert model.row("web-address").qr_value
    assert model.row("tailscale").value is True
    assert model.row("tailscale-address").value == "https://100.64.0.50:8787"
    assert model.row("tailscale-address").qr_value


def test_offline_lan_never_exposes_an_unusable_address_or_qr() -> None:
    model = build_fixture_model(
        ip_address="Offline",
        web_runtime_available=True,
        web_enabled=True,
        web_token="pair-token",
    )

    assert model.row("web-address").available is False
    assert model.row("web-address").value == "Unavailable"
    assert model.row("web-address").qr_value is None


def test_web_access_disable_confirmation_matches_transition() -> None:
    model = build_fixture_model(tailscale_installed=True)
    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None
    assert model.pending_confirmation.message.startswith("Disable")

    model.dispatch(Action.SELECT)
    assert model.row("web-access").value is False
    assert model.row("web-address").available is False
    assert model.row("web-address").reason == "Enable the Web app first."
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "The Web app must be enabled first."


def test_batocera_service_toggle_warns_before_disabling_background_features() -> None:
    model = build_fixture_model(service_enabled=True)
    model.select("batocera-service")

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None
    assert "Web app" in model.pending_confirmation.message
    assert "audio-output blacklist" in model.pending_confirmation.message
    assert model.row("batocera-service").value is True

    model.dispatch(Action.SELECT)
    assert model.row("batocera-service").value is False


def test_back_cancels_confirmation_without_mutating() -> None:
    model = build_fixture_model(previous_version="0.0.9")
    model.select("rollback")

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None

    model.dispatch(Action.BACK)
    assert model.pending_confirmation is None
    assert model.last_action is None


def test_update_section_matches_romm_o_cera_pattern() -> None:
    model = build_fixture_model(previous_version="0.0.9", update_release="1.2.3", notes_available=True)

    assert model.row("update-channel").kind is RowKind.CHOICE
    assert model.row("update-channel").value == "Stable"
    assert model.row("update-channel").choices == ("Stable", "Nightly")
    assert model.row("check-updates-daily").kind is RowKind.TOGGLE
    assert model.row("check-updates-daily").value is True
    assert model.row("check-updates").kind is RowKind.ACTION
    assert model.row("check-updates").label == "Check for updates"
    assert model.row("check-updates").available is True
    assert model.row("check-updates").value == "1.2.3 available"
    assert model.row("whats-new").label == "What's new in this version"
    assert model.row("whats-new").available is True
    assert model.row("rollback").label == "Go back to the previous version"
    assert model.row("rollback").value == "0.0.9"


def test_previous_version_is_unavailable_when_none_is_retained() -> None:
    model = build_fixture_model()

    assert model.row("rollback").available is False
    assert model.row("rollback").reason == "No previous version is stored on this machine."


def test_release_notes_use_a_bounded_paged_overlay() -> None:
    model = build_fixture_model(notes_available=True)
    model.show_overlay("What's new", "\n".join(f"Release note {index}" for index in range(30)))

    assert len(model.overlay_pages) == 3
    assert len(model.overlay_pages[0]) <= 11
    model.dispatch(Action.DOWN)
    assert model.overlay_page == 1
    model.dispatch(Action.BACK)
    assert model.overlay_pages == []


def test_choice_cycles_and_unavailable_rows_do_nothing() -> None:
    model = build_fixture_model()
    model.select("update-channel")
    model.dispatch(Action.SELECT)
    assert model.row("update-channel").value == "Nightly"

    model.select("rollback")
    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is None
    assert model.last_action is None
