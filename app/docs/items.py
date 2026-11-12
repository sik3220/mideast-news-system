"""편집 화면의 본문 텍스트를 항목 목록으로 바꾼다.

형식: 빈 줄로 항목을 나눈다. 항목의 첫 줄이 머리줄이고, '*' 또는 '☞' 로 시작하는 줄이 하위 줄이다.
그 밖의 줄은 바로 앞 줄에 이어 붙인다.
"""
from __future__ import annotations

import math
import re
import unicodedata

SUB_MARKERS = ("*", "☞")


def parse_items(text: str) -> list[dict]:
    items: list[dict] = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        head = re.sub(r"^[-•·]\s*", "", lines[0])
        subs: list[str] = []
        for ln in lines[1:]:
            if ln.startswith(SUB_MARKERS):
                subs.append(ln[0] + " " + ln[1:].strip())
            elif subs:
                subs[-1] += " " + ln
            else:
                head += " " + ln
        items.append({"head": head, "subs": subs})
    return items


def items_to_text(items: list[dict]) -> str:
    return "\n\n".join("\n".join([it["head"], *it.get("subs", [])]) for it in items)


def char_units(ch: str) -> float:
    """글자 폭을 전각 1.0 기준으로 어림한다."""
    if ch == " ":
        return 0.3
    if unicodedata.east_asian_width(ch) in "WF":
        return 1.0
    return 0.55


def text_units(text: str) -> float:
    return sum(char_units(c) for c in text)


def estimate_lines(text: str, font_pt: float, width_pt: float) -> int:
    return max(1, math.ceil(text_units(text) * font_pt / width_pt))


def display_width(text: str) -> int:
    """고정폭 기준 폭(전각 2, 반각 1). Word 원본의 수동 줄바꿈 위치를 맞추는 데 쓴다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def wrap_sub_line(line: str, max_width: int) -> list[str]:
    """하위 줄을 원본처럼 낱말 단위로 끊고 이어지는 줄은 들여쓴다('*' 2칸, '☞' 3칸)."""
    indent = "   " if line.startswith("☞") else "  "
    out, current = [], ""
    for word in line.split(" "):
        candidate = word if not current else current + " " + word
        if current and display_width(candidate) > max_width:
            out.append(current)
            current = indent + word
        else:
            current = candidate
    if current.strip():
        out.append(current)
    return out
