"""Normalize clock typography without changing the user's requested time."""

import re


def normalize_time_text(text: str) -> str:
    """Accept full-width digits/colons and spaces around a numeric clock separator."""
    text = text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))

    def chinese_hour(match):
        raw = match.group(0)
        digits = {
            "零": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if "十" in raw:
            first, last = raw.split("十", 1)
            if len(first) > 1 or len(last) > 1:
                return raw
            return str((digits.get(first, 1) * 10) + digits.get(last, 0))
        return str(digits[raw]) if raw in digits else raw

    text = re.sub(
        r"[零一二两三四五六七八九十]{1,3}(?=点)(?!点[零一二三四五六七八九]+(?:公里|千米|米))",
        chinese_hour,
        text,
    )
    return re.sub(r"(?<=\d)\s*:\s*(?=\d)", ":", text)
