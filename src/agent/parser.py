"""Conservative Chinese fallback parser; uncertain hard constraints require clarification."""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from src.domain import Constraints, Preferences, TZ
from src.errors import NeedsClarification
from src.agent.time_text import normalize_time_text
from src.agent.time_roles import arrival_match, arrival_time, departure_text, earliest_arrival_departure
from src.agent.explicit_limits import explicit_limits


def route_match(text: str):
    """Find a new OD request, including colloquial departure/arrival wording."""
    return re.search(
        r"从\s*([^，,。；;]+?)(?:出发)?\s*(?:[，,]\s*)?(?:到|去(?:前往)?|前往)\s*([^，,。；;]+)", text
    )


def night_clock_assumed(text: str) -> bool:
    return bool(
        re.search(r"末班|赶不上|停运", text)
        and not re.search(r"上午|早上|下午|晚上|凌晨|中午", text)
        and re.search(r"(?<!\d)(?:[7-9]|10|11)(?:点|:\d{2})", text)
    )


def parse(text: str, prefs: Preferences, now: datetime, previous: Constraints | None = None) -> Constraints:
    text = normalize_time_text(text)
    if previous and re.search(
        r"(?:目的地|出发地)(?:改|换)|提前|推迟|延后|晚(?:半|[一二两三\d]+).*小时", text
    ):
        raise NeedsClarification(
            "已收到行程修改，但规则解析无法确定新地点或时刻；请明确写出修改后的出发地、目的地和时间。"
        )
    limits, limits_removed = explicit_limits(text)
    arrival_clause = arrival_match(text)
    required_arrival = arrival_time(arrival_clause, text, now, previous) if arrival_clause else None
    departure_query = departure_text(limits_removed, arrival_match(limits_removed))
    route = route_match(text)
    if route is None:
        route = re.search(
            r"(?:我)?(?:现在)?在\s*([^，,。；;]+)[，,]\s*[^，,。；;]*?(?:要|想|准备)?去\s*([^，,。；;]+)",
            text,
        )
    if not route and previous is None:
        raise NeedsClarification(
            "请提供出发地、目的地和出发时间，例如：明天晚上从北京海淀到天津滨海新区，预算100以内。"
        )
    base = previous.model_dump() if previous else {}
    if route:
        destination = route.group(2).strip()
        if arrival_clause:
            destination = re.sub(
                r"(?:今天|明天|后天|上午|下午|晚上)?\s*\d{1,2}(?:点.*|:\d{2}.*)$", "", destination
            ).rstrip()
        base.update(origin=route.group(1).strip(), destination=destination)
    date_text = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if not date_text and re.search(r"\d{1,2}\s*(?:月|[日号])", text):
        raise NeedsClarification("请明确出行年月日，使用YYYY-MM-DD格式；不能把“26号”等日期当作今天。")
    has_time = (
        any(w in text for w in ("明天", "今天", "后天", "现在", "晚上", "凌晨", "上午", "下午"))
        or bool(re.search(r"\d{1,2}(?::\d{2}|点)", text))
        or bool(date_text)
    )
    if previous is None or has_time:
        local_now = now.astimezone(TZ)
        day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if date_text:
            try:
                day = datetime.strptime(date_text.group(1), "%Y-%m-%d").replace(tzinfo=TZ)
            except ValueError as exc:
                raise NeedsClarification("日期无效，请使用 YYYY-MM-DD。") from exc
        elif "后天" in text:
            day += timedelta(days=2)
        elif "明天" in text:
            day += timedelta(days=1)
        elif previous and "今天" not in text and "现在" not in text:
            day = previous.depart_after.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if not has_time:
            raise NeedsClarification("请补充出发日期和时间。")
        # Arrival clauses must not be mistaken for departure times.
        depart_text = re.split(r"最晚|必须.*?到达|到达时间", departure_query)[0]
        exact = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2})(?!\d)|点(?:(\d{1,2})分?|半)?)", depart_text)
        if exact:
            hour, minute = int(exact.group(1)), int(exact.group(2) or exact.group(3) or 0)
            if exact.group(0).endswith("半"):
                minute = 30
            if ("晚上" in depart_text or "下午" in depart_text or night_clock_assumed(text)) and hour < 12:
                hour += 12
            if "中午" in depart_text and 1 <= hour <= 2:
                hour += 12
            if hour > 23 or minute > 59:
                raise NeedsClarification("时间无效，请使用24小时制，例如23:00。")
            start = day + timedelta(hours=hour, minutes=minute)
            if re.search(r"\d点[一二三四五六七八九十]", depart_text):
                raise NeedsClarification("请把出发时间写成HH:MM，例如23:30。")
            end = start
        elif re.search(r"\d\s*:", depart_text):
            raise NeedsClarification("未能识别指定时刻，请使用HH:MM，例如22:30；不会改为当前时间规划。")
        elif required_arrival:
            # No departure clock was given: search earlier times, never depart at the deadline.
            start = earliest_arrival_departure(text, arrival_clause, required_arrival, now, previous)
            end = required_arrival - timedelta(seconds=1)
            base["arrival_priority"] = True
        elif "现在" in text:
            start, end = local_now, local_now + timedelta(minutes=15)
        else:
            periods = {"晚上": (18, 23, 59), "凌晨": (0, 5, 59), "上午": (6, 11, 59), "下午": (12, 17, 59)}
            match = next((v for word, v in periods.items() if word in text), None)
            if not match:
                raise NeedsClarification("请提供出发时刻或上午、下午、晚上等时间段。")
            start = day + timedelta(hours=match[0])
            end = day + timedelta(hours=match[1], minutes=match[2])
        arrival = end + timedelta(hours=12)
        deadline = re.search(
            r"最晚\s*(?:(次日|第二天|明天)\s*)?(\d{1,2})(?::(\d{2})|点)\s*(?:前)?(?:到|抵达)?", text
        )
        if required_arrival:
            arrival = required_arrival
        elif deadline:
            hour, minute = int(deadline.group(2)), int(deadline.group(3) or 0)
            if hour > 23 or minute > 59:
                raise NeedsClarification("最晚到达时间无效。")
            arrival = day + timedelta(days=1 if deadline.group(1) else 0, hours=hour, minutes=minute)
        elif "最晚" in text:
            raise NeedsClarification("请将最晚到达时间写成最晚23:00到或最晚次日01:00到。")
        if arrival <= end and arrival > start:
            end = arrival - timedelta(seconds=1)
        base.update(depart_after=start, depart_before=end, arrive_by=arrival)
        if exact:
            base["arrival_priority"] = False
    budget = re.search(r"(?:预算|不超过|最多花|上限)\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?", limits_removed)
    if budget:
        base["budget_cents"] = int(Decimal(budget.group(1)) * 100)
    elif "不限预算" not in limits_removed and any(w in limits_removed for w in ("预算", "元以内", "不超过")):
        raise NeedsClarification("请用数字写明预算上限，例如预算100元。")
    if "不限预算" in text:
        base["budget_cents"] = None
    # Unhandled hard wording cannot be silently dropped by the rule fallback.
    _, unhandled = explicit_limits(departure_query)
    if re.search(r"必须|禁止步行|最多\d+次换乘|\d+(?:公里|km)|\d{1,2}:\d{2}\s*[-~至]", unhandled):
        raise NeedsClarification(
            "这条硬约束需要更精确的输入；请在API constraints字段指定时间窗/距离/排除方式。"
        )
    base.update(prefs.model_dump())
    base.update(limits)
    try:
        return Constraints.model_validate(base)
    except ValueError as exc:
        raise NeedsClarification("出发与到达时间冲突，或超出48小时规划范围，请调整时间。") from exc
