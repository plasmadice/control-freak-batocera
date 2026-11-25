import pygame

from control_freak.app import save_screenshot
from control_freak.model import Action, build_fixture_model
from control_freak.ui import FONT_BOLD, FONT_REGULAR, HEIGHT, WIDTH, context_title_lines, font, render


def test_render_produces_full_logical_canvas() -> None:
    pygame.init()
    surface = pygame.Surface((WIDTH, HEIGHT))
    model = build_fixture_model()
    model.select("web-access")

    render(surface, model)

    assert surface.get_size() == (1280, 720)
    assert surface.get_at((10, 10)) != surface.get_at((640, 360))


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


def test_context_titles_fit_field_note_panel() -> None:
    pygame.init()
    model = build_fixture_model()
    for section in model.sections:
        for row in section.rows:
            lines = context_title_lines(row.label)
            assert 1 <= len(lines) <= 2
            assert all(font(21, True).size(line)[0] <= 232 for line in lines)


def test_fonts_are_packaged_assets() -> None:
    assert FONT_REGULAR.is_file()
    assert FONT_BOLD.is_file()


def test_screenshot_can_render_twice_deterministically(tmp_path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    save_screenshot(first)
    save_screenshot(second)

    assert first.read_bytes() == second.read_bytes()
