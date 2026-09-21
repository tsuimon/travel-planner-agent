"""Capability checks before reducing a natural-language request to one OD pair."""

import re


def is_itinerary(text: str) -> bool:
    """Recognize ordered visits before reducing a request to one OD pair."""
    sequenced = re.search(
        r"(?:然后|之后|接着|顺便|再)(?:\s|[，,])*[^，,。；;]{0,10}(?:去|到|吃|看|逛|住|玩)", text
    )
    waypoints = re.search(r"途经|途径|经停|中途停|沿途停", text) or len(re.findall(r"→|->", text)) > 1
    activity = any(word in text for word in ("演唱会", "海底捞", "聚餐", "参加会议"))
    return bool(sequenced or waypoints or activity)
