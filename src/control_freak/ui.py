from __future__ import annotations

from pathlib import Path

import pygame

from .model import AppModel, RowKind, SettingRow
from .text import wrap_text

WIDTH = 1280
HEIGHT = 720
FONT_REGULAR = Path(__file__).parent / "assets" / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = Path(__file__).parent / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

INK = pygame.Color("#081117")
HEADER = pygame.Color("#101c24")
SIDEBAR = pygame.Color("#0d1820")
PANEL = pygame.Color("#13232d")
PANEL_ALT = pygame.Color("#182b36")
RULE = pygame.Color("#29404d")
TEXT = pygame.Color("#f3f0e8")
MUTED = pygame.Color("#94a7b2")
DIM = pygame.Color("#647681")
AMBER = pygame.Color("#f2b84b")
TEAL = pygame.Color("#56d3c2")
RED = pygame.Color("#ef6b64")

_fonts: dict[tuple[int, bool], pygame.font.Font] = {}


def font(size: int, bold: bool = False) -> pygame.font.Font:
    key = (size, bold)
    if key not in _fonts:
        _fonts[key] = pygame.font.Font(FONT_BOLD if bold else FONT_REGULAR, size)
    return _fonts[key]


def clear_font_cache() -> None:
    _fonts.clear()


def context_title_lines(text: str) -> list[str]:
    return wrap_text(text, font(21, True), 232)[:2]


def label(surface: pygame.Surface, text: str, x: int, y: int, size: int, color: pygame.Color = TEXT, bold: bool = False) -> pygame.Rect:
    image = font(size, bold).render(text, True, color)
    return surface.blit(image, (x, y))


def render(surface: pygame.Surface, model: AppModel) -> None:
    surface.fill(INK)
    _header(surface, model)
    _sidebar(surface, model)
    _content(surface, model)
    _footer(surface, model)
    if model.pending_confirmation:
        _modal(surface, model)


def _header(surface: pygame.Surface, model: AppModel) -> None:
    pygame.draw.rect(surface, HEADER, (0, 0, WIDTH, 90))
    pygame.draw.rect(surface, AMBER, (0, 0, 8, 90))
    label(surface, "CONTROL FREAK", 34, 20, 28, TEXT, True)
    label(surface, "BATOCERA SYSTEM CONTROL", 36, 55, 12, MUTED, True)

    pygame.draw.line(surface, RULE, (875, 22), (875, 68), 1)
    label(surface, "MACHINE", 904, 19, 11, MUTED, True)
    label(surface, str(model.row("device").value).upper(), 904, 39, 20, TEXT, True)
    pygame.draw.circle(surface, TEAL, (1174, 43), 6)
    label(surface, "LOCAL", 1190, 34, 14, TEAL, True)


def _sidebar(surface: pygame.Surface, model: AppModel) -> None:
    pygame.draw.rect(surface, SIDEBAR, (0, 90, 322, 574))
    pygame.draw.line(surface, RULE, (321, 90), (321, 664), 1)
    label(surface, "SETTINGS INDEX", 34, 116, 12, MUTED, True)

    y = 150
    for index, section in enumerate(model.sections):
        selected = index == model.section_index
        rect = pygame.Rect(20, y, 282, 82)
        if selected:
            pygame.draw.rect(surface, PANEL_ALT, rect, border_radius=4)
            pygame.draw.rect(surface, AMBER if not model.rows_focused else RULE, (20, y, 4, 82), border_radius=2)
        label(surface, f"{index + 1:02}", 38, y + 17, 13, AMBER if selected else DIM, True)
        label(surface, section.title.upper(), 78, y + 14, 18, TEXT if selected else MUTED, True)
        summary = wrap_text(section.summary, font(12), 198)
        for line_index, line in enumerate(summary[:2]):
            label(surface, line, 78, y + 40 + line_index * 16, 12, MUTED if selected else DIM)
        y += 94


def _content(surface: pygame.Surface, model: AppModel) -> None:
    section = model.current_section
    x = 354
    label(surface, f"SECTION {model.section_index + 1:02}", x, 116, 12, AMBER, True)
    label(surface, section.title, x, 139, 34, TEXT, True)
    label(surface, section.summary, x, 182, 16, MUTED)

    rows_x, rows_y, rows_w, row_h = x, 224, 590, 94
    for index, row in enumerate(section.rows):
        y = rows_y + index * (row_h + 8)
        selected = model.rows_focused and index == model.row_index
        bg = PANEL_ALT if selected else PANEL
        pygame.draw.rect(surface, bg, (rows_x, y, rows_w, row_h), border_radius=5)
        if selected:
            pygame.draw.rect(surface, AMBER, (rows_x, y, rows_w, row_h), 2, border_radius=5)
        elif row.level == "error":
            pygame.draw.rect(surface, RED, (rows_x, y, 4, row_h), border_radius=2)

        row_color = TEXT if row.available else DIM
        label(surface, row.label, rows_x + 20, y + 17, 19, row_color, True)
        _value(surface, row, rows_x + rows_w - 20, y + 19)
        description = row.reason if not row.available else row.description
        for line_index, line in enumerate(wrap_text(description, font(13), 390)[:2]):
            label(surface, line, rows_x + 20, y + 50 + line_index * 17, 13, MUTED if row.available else DIM)

    _context(surface, model, 972, 224, 276, 400)


def _value(surface: pygame.Surface, row: SettingRow, right: int, y: int) -> None:
    if row.kind is RowKind.TOGGLE:
        on = bool(row.value)
        rect = pygame.Rect(right - 70, y - 1, 70, 32)
        pygame.draw.rect(surface, TEAL if on else pygame.Color("#344954"), rect, border_radius=16)
        knob_x = rect.right - 16 if on else rect.left + 16
        pygame.draw.circle(surface, INK if on else MUTED, (knob_x, rect.centery), 11)
        return

    value = str(row.value) if row.value is not None else ""
    color = TEAL if row.level == "good" else RED if row.level == "error" else TEXT if row.available else DIM
    image = font(15, True).render(value.upper(), True, color)
    surface.blit(image, (right - image.get_width(), y + 3))


def _context(surface: pygame.Surface, model: AppModel, x: int, y: int, w: int, h: int) -> None:
    row = model.current_row
    pygame.draw.rect(surface, pygame.Color("#0e1b23"), (x, y, w, h), border_radius=5)
    pygame.draw.rect(surface, RULE, (x, y, w, h), 1, border_radius=5)
    label(surface, "FIELD NOTE", x + 22, y + 20, 11, AMBER, True)
    title_y = y + 51
    title_lines = context_title_lines(row.label)
    for line_index, line in enumerate(title_lines):
        label(surface, line, x + 22, title_y + line_index * 27, 21, TEXT, True)
    line_y = y + 91 + max(0, len(title_lines) - 1) * 27
    for line in wrap_text(row.description, font(15), w - 44):
        label(surface, line, x + 22, line_y, 15, MUTED)
        line_y += 21

    line_y += 16
    pygame.draw.line(surface, RULE, (x + 22, line_y), (x + w - 22, line_y), 1)
    line_y += 22
    label(surface, "CURRENT", x + 22, line_y, 11, MUTED, True)
    line_y += 23
    value = "ON" if row.kind is RowKind.TOGGLE and row.value else "OFF" if row.kind is RowKind.TOGGLE else str(row.value)
    label(surface, value.upper(), x + 22, line_y, 19, TEXT if row.available else DIM, True)

    if not row.available:
        line_y += 48
        label(surface, "UNAVAILABLE", x + 22, line_y, 11, RED, True)
        line_y += 22
        for line in wrap_text(row.reason, font(13), w - 44):
            label(surface, line, x + 22, line_y, 13, MUTED)
            line_y += 18


def _footer(surface: pygame.Surface, model: AppModel) -> None:
    pygame.draw.rect(surface, HEADER, (0, 664, WIDTH, 56))
    pygame.draw.line(surface, RULE, (0, 664), (WIDTH, 664), 1)
    hints = [("↑↓", "MOVE"), ("←→", "CHANGE AREA"), ("A / ENTER", "SELECT"), ("B / ESC", "BACK")]
    x = 34
    for key, meaning in hints:
        label(surface, key, x, 684, 13, TEXT, True)
        x += font(13, True).size(key)[0] + 9
        label(surface, meaning, x, 684, 11, MUTED, True)
        x += font(11, True).size(meaning)[0] + 34
    label(surface, "PROTOTYPE · NO SYSTEM CHANGES", 1016, 684, 11, DIM, True)


def _modal(surface: pygame.Surface, model: AppModel) -> None:
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((2, 8, 12, 208))
    surface.blit(overlay, (0, 0))
    confirmation = model.pending_confirmation
    if confirmation is None:
        return

    rect = pygame.Rect(372, 194, 536, 332)
    pygame.draw.rect(surface, PANEL_ALT, rect, border_radius=8)
    pygame.draw.rect(surface, AMBER, rect, 2, border_radius=8)
    label(surface, "CONFIRM CHANGE", rect.x + 34, rect.y + 30, 12, AMBER, True)
    label(surface, confirmation.title, rect.x + 34, rect.y + 61, 27, TEXT, True)
    y = rect.y + 110
    for line in wrap_text(confirmation.message, font(17), rect.w - 68):
        label(surface, line, rect.x + 34, y, 17, MUTED)
        y += 24

    pygame.draw.rect(surface, AMBER, (rect.x + 34, rect.bottom - 74, 215, 42), border_radius=4)
    label(surface, "A / ENTER  CONFIRM", rect.x + 51, rect.bottom - 63, 14, INK, True)
    pygame.draw.rect(surface, PANEL, (rect.x + 266, rect.bottom - 74, 236, 42), border_radius=4)
    pygame.draw.rect(surface, RULE, (rect.x + 266, rect.bottom - 74, 236, 42), 1, border_radius=4)
    label(surface, "B / ESC  CANCEL", rect.x + 285, rect.bottom - 63, 14, TEXT, True)
