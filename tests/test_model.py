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
    assert row_ids.isdisjoint({"rom-scan", "resolution", "restart-emulationstation", "reboot"})


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


def test_web_access_requires_confirmation_and_updates_dependents() -> None:
    model = build_fixture_model(ip_address="192.168.1.50", tailscale_installed=True, web_runtime_available=True)
    model.select("web-access")

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None
    assert model.pending_confirmation.message.startswith("Enable")
    assert model.row("web-access").value is False

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is None
    assert model.row("web-access").value is True
    assert model.row("web-address").available is True
    assert model.row("web-address").value == "http://192.168.1.50:8787"
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
    assert model.row("web-address").reason == "Web service is not available in this build."


def test_tailscale_requires_installation_and_web_access() -> None:
    model = build_fixture_model(tailscale_installed=False)
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "Tailscale is not installed or connected on this machine."

    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "Web service is not available in this build."


def test_tailscale_address_stays_hidden_until_its_toggle_is_enabled() -> None:
    model = build_fixture_model(tailscale_address="100.64.0.50", web_runtime_available=True)
    model.select("web-access")
    model.dispatch(Action.SELECT)
    model.dispatch(Action.SELECT)

    assert model.row("tailscale").available is True
    assert model.row("tailscale-address").available is False
    assert model.row("tailscale-address").value == "Hidden"

    model.select("tailscale")
    model.dispatch(Action.SELECT)
    assert model.row("tailscale-address").available is True
    assert model.row("tailscale-address").value == "http://100.64.0.50:8787"


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
    assert model.row("web-address").reason == "Enable Web access first."
    assert model.row("tailscale").available is False
    assert model.row("tailscale").reason == "Web access must be enabled first."


def test_back_cancels_confirmation_without_mutating() -> None:
    model = build_fixture_model()
    model.select("install-stable")

    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is not None

    model.dispatch(Action.BACK)
    assert model.pending_confirmation is None
    assert model.last_action is None


def test_update_section_exposes_stable_and_nightly_actions() -> None:
    model = build_fixture_model()

    assert model.row("install-stable").kind is RowKind.ACTION
    assert model.row("install-nightly").kind is RowKind.ACTION
    assert model.row("install-stable").confirm.startswith("Install the staged stable")
    assert model.row("install-nightly").confirm.startswith("Install the staged nightly")
    assert model.row("update-status").value == "Not checked"
    assert model.row("install-stable").field_label == "RELEASE ACTION"
    assert model.row("install-stable").field_value == "INSTALL STAGED STABLE"
    assert model.row("install-nightly").field_value == "INSTALL STAGED NIGHTLY"


def test_choice_cycles_and_unavailable_rows_do_nothing() -> None:
    model = build_fixture_model()
    model.select("audio-output")
    model.dispatch(Action.SELECT)
    assert model.row("audio-output").value == "Analog"

    model.select("rollback")
    model.dispatch(Action.SELECT)
    assert model.pending_confirmation is None
    assert model.last_action is None
