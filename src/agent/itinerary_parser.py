"""Multi-stop draft extraction and clarification, with optional LLM interpretation."""

import re
from datetime import datetime, timedelta
from decimal import Decimal

from src.domain import ItineraryDraft, ItineraryStop, Preferences, TZ
from src.agent.time_text import normalize_time_text
from src.agent.explicit_limits import explicit_limits


def preserve_known_fields(
    predicted: ItineraryDraft, known: ItineraryDraft, previous: ItineraryDraft | None
) -> ItineraryDraft:
    """Explicit rule updates win; omitted historical facts survive model extraction."""
    for name in (
        "origin",
        "depart_after",
        "depart_before",
        "arrive_by",
        "budget_cents",
        "allow_overnight",
        "max_walk_m",
        "max_bike_m",
        "max_transfers",
    ):
        if name in {"max_walk_m", "max_bike_m", "max_transfers"} and name not in known.model_fields_set:
            continue
        value = getattr(known, name)
        old = getattr(previous, name) if previous else None
        if previous and old is not None and value is None:
            # The rule draft is copied from history; a removed field is an explicit clear operation.
            setattr(predicted, name, None)
            continue
        if value is not None and (value != old or getattr(predicted, name) is None):
            setattr(predicted, name, value)
            if name == "budget_cents":
                predicted.budget_scope = known.budget_scope
    if len(predicted.stops) == len(known.stops):
        for index, (actual, model_stop) in enumerate(zip(known.stops, predicted.stops)):
            old_stop = previous.stops[index] if previous and index < len(previous.stops) else None
            for name in ("locations", "start_at", "duration_min", "cost_cents"):
                value = getattr(actual, name)
                old = getattr(old_stop, name) if old_stop else None
                model_value = getattr(model_stop, name)
                if (
                    value is not None
                    and value != []
                    and (value != old or model_value is None or model_value == [])
                ):
                    setattr(model_stop, name, value)
            model_stop.label = actual.label
            model_stop.requires_start_time |= actual.requires_start_time
            if not model_stop.requires_start_time and actual.start_at is None:
                # A flexible activity begins after travel, not automatically at the preceding event's end.
                model_stop.start_at = None
    return ItineraryDraft.model_validate(predicted.model_dump())


def missing_fields(draft: ItineraryDraft) -> list[str]:
    missing = []
    if not draft.origin:
        missing.append("出发地点")
    if not draft.depart_after:
        missing.append("出发的具体年月日和时间（例如2026-09-26 15:00）")
    if not draft.arrive_by:
        missing.append("最后一站最晚到达的年月日和时间")
    if not draft.stops:
        missing.append("按顺序需要到访的地点")
    for stop in draft.stops:
        if not stop.locations or any(x in {"海底捞", "餐厅", "饭店", "酒店"} for x in stop.locations):
            missing.append(f"{stop.label}的具体地点或可接受的几个候选地点")
        if stop.requires_start_time and stop.start_at is None:
            missing.append(f"{stop.label}的开始时间（含日期）")
        if stop.duration_min is None:
            missing.append(f"{stop.label}预计持续多少分钟（例如散场前时长或用餐时长）")
        if draft.budget_scope == "total" and stop.duration_min and stop.cost_cents is None:
            missing.append(f"{stop.label}需计入总预算的费用；已付款不计入可填0")
    if draft.allow_overnight is None:
        missing.append("是否接受跨午夜出行或过夜")
    return missing


def explain_draft(draft: ItineraryDraft, data_mode: str) -> str:
    route = [draft.origin or "待确认起点"] + [
        f"{' / '.join(s.locations) or '待选地点'}（{s.label}）" for s in draft.stops
    ]
    parts = ["已按顺序记录这次行程：" + " → ".join(route) + "。"]
    if draft.preferences.budget_preference == "economy":
        parts.append("本次以省钱为优先目标；会把活动、用餐和后续交通的时间连起来计算。")
    missing = missing_fields(draft)
    if missing:
        parts.append(
            "还需要补充以下信息，我会保留已提供的内容，补充后继续规划：\n\n"
            + "\n".join(f"- {item}" for item in missing)
        )
    if data_mode == "demo":
        parts.append("当前使用演示数据，真实地址和班次尚未接入；记录行程不代表已核验车次和报价。")
    return "\n\n".join(parts)


def rule_draft(text: str, prefs: Preferences, previous: ItineraryDraft | None = None) -> ItineraryDraft:
    """Conservative fallback for ordered clauses; never invent activity times."""
    text = normalize_time_text(text)
    draft = previous.model_copy(deep=True) if previous else ItineraryDraft(preferences=prefs)
    draft.preferences = prefs
    for name, value in explicit_limits(text)[0].items():
        setattr(draft, name, value)
    origin = re.search(r"(?:我现在在|我在|从)([^，,。；;]+?)(?=[，,。；;]|到|出发)", text)
    if origin:
        draft.origin = origin.group(1).strip()
    if not previous or re.search(r"(?:重新规划|改为从|新行程)", text):
        stops = []
        for clause in re.split(r"[，,。；;]", text):
            event = re.search(r"(?:去|到|在)(.+?)(?:看|参加)(演唱会|会议|比赛|演出)", clause)
            meal = re.search(r"吃(?:[一二两三\d]+(?:个)?小时|\d+分钟)?(.+?)(?=后(?:去|到)|$)", clause)
            destination = re.search(r"(?:然后|再|接着|之后|后)(?:去|到)(.+)", clause)
            if event:
                stops.append(
                    ItineraryStop(
                        label=event.group(2), locations=[event.group(1).strip()], requires_start_time=True
                    )
                )
            elif meal:
                stops.append(ItineraryStop(label="用餐", locations=[meal.group(1).strip()]))
            if destination and not event:
                stops.append(
                    ItineraryStop(label="到达", locations=[destination.group(1).strip()], duration_min=0)
                )
        if stops:
            draft.stops = stops
    apply_chinese_times(text, draft)
    # Explicit ISO times are supported offline; flexible phrasing is handled by the LLM.
    stamp = r"(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\+08:00)?)"

    def to_time(value: str) -> datetime:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=TZ)

    departure = re.search(r"出发(?:时间)?[：:\s]*" + stamp, text) or re.search(stamp + r"\s*出发", text)
    if departure:
        draft.depart_after = to_time(departure.group(1))
        draft.depart_before = draft.depart_after + timedelta(minutes=15)
    arrival = re.search(r"最晚(?:到达)?[：:\s]*" + stamp, text)
    if arrival:
        draft.arrive_by = to_time(arrival.group(1))
    for stop in draft.stops:
        label = re.escape(stop.label)
        start = re.search(label + r"(?:开始)?[：:\s]*" + stamp, text)
        if start:
            stop.start_at = to_time(start.group(1))
        duration = re.search(label + r"(?:持续|时长|时间)?[：:\s]*(\d+)\s*分钟", text)
        if duration:
            stop.duration_min = int(duration.group(1))
        place = re.search(label + r"(?:地点|门店)[：:\s]*([^，,。；;]+)", text)
        if place:
            stop.locations = [place.group(1).strip()]
        cost = re.search(label + r"(?:费用|预算)[：:\s]*(\d+(?:\.\d{1,2})?)元", text)
        if cost:
            stop.cost_cents = int(Decimal(cost.group(1)) * 100)
    budget = re.search(r"(?:总预算|交通预算)[：:\s]*(\d+(?:\.\d{1,2})?)", text)
    if budget:
        draft.budget_cents = int(Decimal(budget.group(1)) * 100)
        draft.budget_scope = "total" if "总预算" in budget.group(0) else "transport"
    if re.search(r"(?:接受|允许|可以)过夜", text):
        draft.allow_overnight = True
    if re.search(r"(?:不接受|不允许|不能|不要)过夜", text):
        draft.allow_overnight = False
    if "只算交通" in text:
        draft.budget_scope = "transport"
    if re.search(
        r"取消(?:最晚)?到达(?:时间)?限制|不限(?:制)?(?:最晚)?到达时间|没有最晚到达(?:时间)?限制", text
    ):
        draft.arrive_by = None
    if re.search(r"取消预算(?:上限|限制)?|预算不限|不限预算", text):
        draft.budget_cents = None
    return ItineraryDraft.model_validate(draft.model_dump(exclude_unset=True))


def apply_chinese_times(text: str, draft: ItineraryDraft) -> None:
    """Handle common explicit Chinese dates and event/meal clocks before the model boundary."""
    calendar = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    day = datetime(*map(int, calendar.groups()), tzinfo=TZ) if calendar else draft.depart_after

    def clock(clause: str, reference: datetime | None) -> datetime | None:
        match = re.search(r"(\d{1,2})(?:[:：](\d{2})|点(半|\d{1,2}分?)?)", clause)
        if not match or reference is None:
            return None
        hour = int(match.group(1))
        minute = (
            int(match.group(2))
            if match.group(2)
            else (30 if match.group(3) == "半" else int((match.group(3) or "0").rstrip("分")))
        )
        if any(w in clause[: match.start()] for w in ("下午", "晚上", "傍晚")) and hour < 12:
            hour += 12
        return reference.replace(hour=hour, minute=minute, second=0, microsecond=0)

    for clause in re.split(r"[，,。；;]", text):
        if "出发" in clause:
            at = clock(clause, day)
            if at:
                draft.depart_after = at
                draft.depart_before = at + timedelta(minutes=15)
                day = at
        for stop in draft.stops:
            if stop.requires_start_time and stop.label in clause:
                at = clock(clause, day)
                if at:
                    stop.start_at = at
            if stop.requires_start_time and "散场" in clause and stop.start_at:
                end = clock(clause, stop.start_at)
                if end:
                    if end < stop.start_at:
                        end += timedelta(days=1)
                    stop.duration_min = int((end - stop.start_at).total_seconds() / 60)
            if stop.label == "用餐" and re.search(r"吃|用餐", clause):
                if re.search(r"用餐|吃饭|吃海底捞", clause):
                    meal_start = clock(clause, day)
                    if meal_start:
                        stop.start_at = meal_start
                        stop.requires_start_time = True
                duration = re.search(r"(?:吃|用餐)([一二两三\d]+)(?:个)?小时", clause)
                if duration:
                    number = duration.group(1)
                    stop.duration_min = {"一": 60, "二": 120, "两": 120, "三": 180}.get(number, 0) or int(
                        number
                    ) * 60
