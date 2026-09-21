"""Extract numeric hard limits separately from soft preferences and model interpretation."""

import re
from decimal import Decimal

NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+(?:点[零一二三四五六七八九]+)?)"
DIGITS = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))


def amount(raw: str) -> Decimal:
    """Convert explicit decimal/Chinese quantities; never infer a quantity from context."""
    if raw[0].isdigit():
        return Decimal(raw)
    whole, _, fraction = raw.partition("点")
    total, section, digit = 0, 0, 0
    for char in whole:
        if char in DIGITS:
            digit = DIGITS[char]
        elif char == "万":
            total += (section + digit) * 10000
            section, digit = 0, 0
        else:
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    decimals = "".join(str(DIGITS[c]) for c in fraction)
    return Decimal(str(total + section + digit) + ("." + decimals if decimals else ""))


def explicit_limits(text: str) -> tuple[dict, str]:
    values = {}
    remaining = text
    for field, activity in (("max_walk_m", r"步行|走路|走"), ("max_bike_m", r"骑行|骑车")):
        pattern = (
            rf"(?:(?:{activity})\s*(?:距离)?\s*(?:最多|不超过|上限|不得超过|只能|至多)"
            rf"|(?:只能|最多|至多)(?:接受)?(?:{activity}))\s*({NUMBER})\s*(公里|千米|km|米)"
        )
        found = re.search(pattern, remaining)
        if found:
            values[field] = int(amount(found[1]) * (1000 if found[2] != "米" else 1))
            remaining = remaining[: found.start()] + remaining[found.end() :]
    match = re.search(
        rf"(?:最多|不超过)\s*({NUMBER})\s*次换乘|(?:换乘(?:最多|不超过)|最多换(?:乘)?)\s*({NUMBER})\s*次",
        remaining,
    )
    if match:
        values["max_transfers"] = int(amount(match[1] or match[2]))
        remaining = remaining[: match.start()] + remaining[match.end() :]
    if "不换乘" in remaining:
        values["max_transfers"] = 0
    return values, remaining
