"""Keep arrival deadlines separate from departure clocks before model interpretation."""

import re
from datetime import datetime, timedelta

from src.domain import TZ
from src.errors import AmbiguousArrivalTime, NeedsClarification

DATE = r"(?:\d{4}-\d{2}-\d{2}|今天|明天|后天|次日|第二天)"
PERIOD = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)"
CLOCK = r"(?P<h>\d{1,2})(?::(?P<m>\d{2})(?!\d)|点(?P<zh>半|\d{1,2}分?)?)"
STAMP = rf"(?P<date>{DATE})?\s*(?P<period>{PERIOD})?\s*{CLOCK}"
ARRIVAL = re.compile(
    rf"(?P<prefix>最晚(?:到达)?|到达时间(?:是|为)?|必须在|要在)?\s*{STAMP}"
    r"\s*(?:之前|以前|前)?\s*(?:必须|一定要|要|得)?\s*(?P<verb>到达|抵达|到)(?!\d|时候|时)"
)
NAMED_ARRIVAL = re.compile(
    rf"(?P<prefix>最晚到达(?:时间)?|到达时间|抵达时间|最晚)\s*(?:是|为|在|改为|改成)?\s*{STAMP}"
)


def arrival_match(text: str):
    matches = sorted([*ARRIVAL.finditer(text), *NAMED_ARRIVAL.finditer(text)], key=lambda m: m.end())
    return next(
        (m for m in reversed(matches) if not re.search(r"(?:不是|不要|并非)\s*$", text[: m.start()])),
        None,
    )


def departure_text(text: str, arrival=None) -> str:
    """Remove arrival and explicitly negated departure clocks before selecting a departure."""
    if arrival:
        text = text[: arrival.start()] + text[arrival.end() :]
    return re.sub(
        rf"(?:不是|并非|不要)(?:在)?\s*(?:{DATE})?\s*(?:{PERIOD})?\s*{CLOCK}\s*(?:出发|走)",
        "",
        text,
    )


def arrival_time(match, text: str, now: datetime, previous=None) -> datetime:
    """A bare 4 o'clock remains ambiguous; an explicit 04:00 is a 24-hour clock."""
    hour = int(match["h"])
    minute = int(match["m"] or (30 if match["zh"] == "半" else (match["zh"] or "0").rstrip("分")))
    period = match["period"]
    if not period and hour < 12 and len(match["h"]) == 1:
        # A period stated earlier in the same clause can still qualify the arrival clock.
        clause = text[: match.start()]
        periods = re.findall(PERIOD, clause)
        period = periods[-1] if periods else None
        if not period:
            raise AmbiguousArrivalTime(
                f"已识别为到达时间。你说的{hour}点是上午还是下午？请补充，例如“下午{hour}点到”。"
            )
    if period in {"下午", "晚上", "傍晚"} and hour < 12:
        hour += 12
    if period in {"凌晨", "上午", "早上"} and hour == 12:
        hour = 0
    if period == "中午" and 1 <= hour <= 2:
        hour += 12
    if hour > 23 or minute > 59:
        raise NeedsClarification("到达时间无效，请使用24小时制，例如16:00到。")
    local = now.astimezone(TZ)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    token = match["date"]
    if not token:
        dates = re.findall(DATE, text[: match.start()])
        token = dates[0] if dates else None
    if token and re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        day = datetime.strptime(token, "%Y-%m-%d").replace(tzinfo=TZ)
    elif token in {"次日", "第二天"}:
        anchor = previous.depart_after if previous else day
        # Explicit departure day in this request takes precedence over old history.
        before = text[: match.start()]
        if "明天" in before:
            anchor = day + timedelta(days=1)
        elif "后天" in before:
            anchor = day + timedelta(days=2)
        elif "今天" in before:
            anchor = day
        day = anchor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif token in {"明天", "后天"}:
        day += timedelta(days=1 if token == "明天" else 2)
    elif not token and previous:
        day = previous.arrive_by.replace(hour=0, minute=0, second=0, microsecond=0)
    return day.replace(hour=hour, minute=minute)


def apply_period_reply(pending: str, reply: str) -> str | None:
    """Resume a pending arrival request when the user answers just '下午'."""
    period = re.fullmatch(rf"\s*(?:是|指的是)?(?P<period>{PERIOD})(?:\d{{1,2}}点(?:到)?)?[。！!]?\s*", reply)
    match = arrival_match(pending)
    if period and match:
        pos = match.start("h")
        return pending[:pos] + period["period"] + pending[pos:]
    return None


def earliest_arrival_departure(
    text: str, match, deadline: datetime, now: datetime, previous=None
) -> datetime:
    """Respect a departure date stated outside the arrival clause when searching backwards."""
    local = now.astimezone(TZ)
    start = max(local, deadline - timedelta(hours=24))
    remaining = departure_text(text, match)
    date = re.search(DATE, remaining)
    if date and date[0] not in {"次日", "第二天"}:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date[0]):
            day = datetime.strptime(date[0], "%Y-%m-%d").replace(tzinfo=TZ)
        else:
            day = local.replace(hour=0, minute=0, second=0, microsecond=0)
            day += timedelta(days={"今天": 0, "明天": 1, "后天": 2}[date[0]])
        start = max(start, day)
    if previous and not previous.arrival_priority and not re.search(r"不是.*出发|并非.*出发", text):
        start = max(start, previous.depart_after)
    return start


def ground_model_times(values: dict, text: str, now: datetime, previous=None) -> dict:
    """User arrival semantics override model guesses, even when route wording needed an LLM."""
    match = arrival_match(text)
    if not match:
        return values
    deadline = arrival_time(match, text, now, previous)
    result = dict(values)
    result["arrive_by"] = deadline
    remaining = departure_text(text, match)
    if not re.search(CLOCK, remaining):
        start = earliest_arrival_departure(text, match, deadline, now, previous)
        result.update(
            depart_after=start, depart_before=deadline - timedelta(seconds=1), arrival_priority=True
        )
    return result
