"""Ground unambiguous departure clocks; this validates facts, not conversation intent."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.agent.time_roles import STAMP, arrival_match, arrival_time
from src.agent.time_text import normalize_time_text
from src.domain import ItineraryDraft, TZ
from src.errors import NeedsClarification


@dataclass(frozen=True)
class DepartureFact:
    at: datetime
    source: str
    period_source: str | None = None


def normalized(text: str) -> str:
    text = normalize_time_text(text)
    text = re.sub(
        r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]",
        lambda m: f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}",
        text,
    )
    for short, full in {
        "今晚": "今天晚上",
        "今夜": "今天晚上",
        "明晚": "明天晚上",
        "今早": "今天早上",
    }.items():
        text = text.replace(short, full)
    return text


def stamp_time(match, text: str, now: datetime, inherited: datetime | None = None) -> datetime | None:
    """A bare twelve-hour clock can inherit period/date only from a user's earlier clock."""
    hour = int(match["h"])
    minute = int(match["m"] or (30 if match["zh"] == "半" else (match["zh"] or "0").rstrip("分")))
    period = match["period"]
    if not period and hour < 12 and not match["h"].startswith("0"):
        if inherited is None:
            return None
        hour = hour % 12 + (12 if inherited.hour >= 12 else 0)
    elif period in {"下午", "晚上", "傍晚"} and hour < 12:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    day = (inherited or now).astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    token = match["date"]
    if not token:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}|今天|明天|后天", text[: match.start()])
        token = dates[-1] if dates else None
    if token:
        day = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            day = datetime.fromisoformat(token).replace(tzinfo=TZ)
        else:
            day += timedelta(days={"今天": 0, "明天": 1, "后天": 2, "次日": 1, "第二天": 1}[token])
    return day.replace(hour=hour, minute=minute)


def departure_fact(
    query: str, history: list[str], now: datetime, reference: datetime | None = None
) -> DepartureFact | None:
    """Recognize clear clock-to-departure bindings, never infer a route or select a tool.

    Ambiguous clocks, negated instructions, ranges and multiple departures remain
    the model's responsibility to clarify. Only user messages supply AM/PM context.
    """
    text = normalized(query)
    # These calendars need semantic resolution; never replace them with today.
    if re.search(r"(?:周|星期|礼拜)[一二三四五六日天]|\d{1,2}月|\d{1,2}[日号](?!线)|大后天", text):
        return None
    facts = []
    for match in re.finditer(STAMP, text):
        before = re.split(r"[，,。；;\n]", text[: match.start()])[-1]
        after = text[match.end() :]
        if re.search(r"[-~～—至到]\s*$", before) and re.search(STAMP, before):
            continue
        if re.search(r"(?:不是|并非|不要|别按|不要按|取消|不在|最晚|最早|大约|约)\s*$", before):
            continue
        if not (
            re.match(r"\s*(?:出发|动身|启程|走(?:[，,。！!]|$))", after)
            or re.match(r"\s*从[^，,。；;\n\d]{1,60}?(?:回|去|前往|出发)", after)
            or re.search(r"(?:出发|动身|启程)(?:时间)?(?:是|为|改为|改成)?\s*$", before)
        ):
            continue
        inherited, period_source = None, None
        if not match["period"]:
            for prior in reversed(history):
                previous = normalized(prior)
                stamps = list(re.finditer(STAMP, previous))
                # Multiple different clock roles need semantic disambiguation.
                for old in reversed(stamps):
                    if int(old["h"]) % 12 == int(match["h"]) % 12:
                        inherited = stamp_time(old, previous, now)
                        if inherited:
                            if reference:
                                # The stored trip date is already resolved: do not
                                # apply “明天” a second time to tomorrow's date.
                                inherited = inherited.replace(
                                    year=reference.year, month=reference.month, day=reference.day
                                )
                            period_source = prior
                            break
                if inherited:
                    break
        at = stamp_time(match, text, now, inherited)
        if at:
            facts.append(DepartureFact(at, query, period_source))
    return facts[0] if len(facts) == 1 else None


def grounded_patch(
    base: ItineraryDraft, patch: dict, fact: DepartureFact | None, user_messages: list[str], now: datetime
) -> dict:
    """Apply a fixed departure atomically, including its obsolete derived state.

    Keep a genuinely user-specified arrival deadline. Remove the same clock copied
    into arrive_by by a model when there is no user evidence of an arrival request.
    """
    if fact is None:
        return patch
    result = dict(patch)
    result.update(depart_after=fact.at.isoformat(), depart_before=fact.at.isoformat(), arrival_priority=False)
    deadline = patch.get("arrive_by", base.arrive_by)
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline)
    departure_deadline = deadline == fact.at
    if deadline and not departure_deadline:
        for i, message in enumerate(user_messages[:-1]):
            old_fact = departure_fact(message, user_messages[:i], now)
            departure_deadline |= bool(old_fact and old_fact.at == deadline)
    if departure_deadline:
        supported = False
        for message in user_messages:
            text = normalized(message)
            found = arrival_match(text)
            if found:
                try:
                    supported |= arrival_time(found, text, now) == deadline
                except (ValueError, NeedsClarification):
                    pass
        if not supported:
            result["arrive_by"] = None
    return result
