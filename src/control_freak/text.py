from __future__ import annotations


def wrap_text(text: str, font: object, max_width: int) -> list[str]:
    """Wrap text by words, splitting only tokens wider than the measure."""
    if not text:
        return []

    def width(value: str) -> int:
        return font.size(value)[0]  # type: ignore[attr-defined]

    words: list[str] = []
    for word in text.split():
        if "-" in word and width(word) > max_width:
            prefix, suffix = word.split("-", 1)
            if prefix and suffix and width(f"{prefix}-") <= max_width:
                words.append(f"{prefix}-")
                word = suffix
        while width(word) > max_width:
            cut = 1
            while cut < len(word) and width(word[: cut + 1]) <= max_width:
                cut += 1
            words.append(word[:cut])
            word = word[cut:]
        if word:
            words.append(word)

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if width(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
