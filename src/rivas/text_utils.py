from __future__ import annotations

from collections.abc import Iterable
import re


MAX_BALE_TEXT = 3900


def split_text(text: str, limit: int = MAX_BALE_TEXT) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n"):
        candidate = paragraph.strip()
        if not candidate:
            if current:
                if len(current) + 1 <= limit:
                    current += "\n"
                else:
                    chunks.append(current.rstrip())
                    current = ""
            continue

        if len(candidate) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.extend(_split_long_line(candidate, limit))
            continue

        potential = f"{current}\n{candidate}".strip() if current else candidate
        if len(potential) <= limit:
            current = potential
        else:
            chunks.append(current.rstrip())
            current = candidate

    if current:
        chunks.append(current.rstrip())

    return [chunk for chunk in chunks if chunk]


def _split_long_line(line: str, limit: int) -> list[str]:
    words = line.split(" ")
    pieces: list[str] = []
    current = ""

    for word in words:
        if not word:
            continue
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            pieces.append(current)
            current = ""

        if len(word) <= limit:
            current = word
        else:
            pieces.extend(_hard_split(word, limit))

    if current:
        pieces.append(current)

    return pieces


def _hard_split(value: str, limit: int) -> list[str]:
    return [value[i : i + limit] for i in range(0, len(value), limit)]


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def enforce_rivas_branding(text: str) -> str:
    """Replace Mira branding with Rivas using lightweight regex."""
    if not text:
        return text

    return re.sub(r"(?i)\bmira\b|میرا", "ریواس", text)
