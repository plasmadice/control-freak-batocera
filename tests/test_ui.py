import pygame
import sys
from types import SimpleNamespace

from control_freak.app import save_screenshot
from control_freak.model import Action, build_fixture_model
from control_freak.ui import FONT_BOLD, FONT_REGULAR, HEIGHT, WIDTH, _draw_qr, _visible_rows, context_title_lines, context_value_lines, font, render


def test_render_produces_full_logical_canvas() -> None:
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model()
    model.select("web-access")

    render(surface, model)

    assert surface.get_size() == (1280, 720)
    assert surface.get_at((10, 10)) != surface.get_at((640, 360))


def test_long_sections_scroll_to_keep_the_selected_row_visible() -> None:
    model = build_fixture_model(previous_version="0.0.9")
    model.select("rollback")

    start, rows = _visible_rows(model)

    assert start == 1
    assert [row.id for row in rows][-1] == "rollback"


def test_confirmation_renders_over_existing_screen() -> None:
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model()
    model.select("web-access")
    render(surface, model)
    before = surface.get_at((100, 100))

    model.dispatch(Action.SELECT)
    render(surface, model)

    assert model.pending_confirmation is not None
    assert surface.get_at((100, 100)) != before


def test_release_notes_overlay_renders_over_existing_screen() -> None:
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model(notes_available=True)
    render(surface, model)
    before = surface.get_at((100, 100))

    model.show_overlay("What's new", "Readable authenticated release notes.")
    render(surface, model)

    assert surface.get_at((100, 100)) != before


def test_context_titles_fit_field_note_panel() -> None:
    pygame.init()
    model = build_fixture_model()
    for section in model.sections:
        for row in section.rows:
            lines = context_title_lines(row.label)
            assert 1 <= len(lines) <= 2
            assert all(font(21, True).size(line)[0] <= 232 for line in lines)


def test_field_note_values_wrap_inside_panel() -> None:
    pygame.init()
    lines = context_value_lines("Ethernet · 192.168.100.200")
    assert 1 <= len(lines) <= 2
    assert all(font(19, True).size(line)[0] <= 232 for line in lines)


def test_fonts_are_packaged_assets() -> None:
    assert FONT_REGULAR.is_file()
    assert FONT_BOLD.is_file()


def test_screenshot_can_render_twice_deterministically(tmp_path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    save_screenshot(first)
    save_screenshot(second)

    assert first.read_bytes() == second.read_bytes()


def test_qr_code_is_drawn_from_pairing_url(monkeypatch) -> None:
    class FakeQR:
        def __init__(self, **_kwargs):
            pass

        def add_data(self, _value):
            pass

        def make(self, **_kwargs):
            pass

        def get_matrix(self):
            return [[True, False], [False, True]]

    monkeypatch.setitem(sys.modules, "qrcode", SimpleNamespace(QRCode=FakeQR))
    surface = pygame.Surface((40, 40))

    assert _draw_qr(surface, "http://example/pair", pygame.Rect(0, 0, 40, 40)) is True
    assert surface.get_at((5, 5)) == pygame.Color("black")
    assert surface.get_at((25, 5)) == pygame.Color("white")
