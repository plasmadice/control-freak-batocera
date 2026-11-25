from control_freak.text import wrap_text


class FakeFont:
    def size(self, text: str) -> tuple[int, int]:
        return len(text) * 10, 20


def test_wrap_text_preserves_words_and_width() -> None:
    lines = wrap_text("Controller-first settings without a web dashboard", FakeFont(), 130)

    assert lines == ["Controller-", "first", "settings", "without a web", "dashboard"]
    assert all(FakeFont().size(line)[0] <= 130 for line in lines)


def test_wrap_text_breaks_oversized_tokens_without_blank_lines() -> None:
    lines = wrap_text("supercalifragilistic", FakeFont(), 50)

    assert lines == ["super", "calif", "ragil", "istic"]
    assert "" not in lines
