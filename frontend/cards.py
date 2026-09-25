"""Accessible, escaped route summaries shared by desktop and mobile."""

from html import escape
from src.domain import Plan
from src.risk import MODE_NAMES
from src.agent.intelligent import PlanningReport, display_options


def duration(minutes: float) -> str:
    value = max(0, round(minutes))
    hours, rest = divmod(value, 60)
    return f"{hours}小时{rest}分" if hours and rest else (f"{hours}小时" if hours else f"{rest}分钟")


def notice(title: str, lines: list[str]) -> str:
    return (
        f'<div class="trip-notice" role="status"><h3>{escape(title)}</h3><ul>'
        + "".join(f"<li>{escape(line)}</li>" for line in lines)
        + "</ul></div>"
    )


def metrics(cost: int, minutes: float, transfers: int, *, estimate: bool = False) -> str:
    label = "已知交通费用" if estimate else "交通参考费用"
    return (
        f'<div class="trip-metrics"><div><small>{label}</small><strong class="trip-price">¥{cost / 100:.2f}</strong></div>'
        f"<div><small>全程用时</small><strong>{duration(minutes)}</strong></div>"
        f"<div><small>换乘次数</small><strong>{transfers} 次</strong></div></div>"
    )


def notes_detail(notes: list[str]) -> str:
    unique = list(dict.fromkeys(n for n in notes if n))
    if not unique:
        return ""
    return (
        "<details><summary>查看测算条件与数据说明</summary><ul>"
        + "".join(f"<li>{escape(n)}</li>" for n in unique)
        + "</ul></details>"
    )


def render_cards(plans: list[Plan]) -> str:
    if not plans:
        return ""
    cards = []
    for index, plan in enumerate(plans):
        legs = "".join(
            f'<li><span class="trip-time">{leg.departure:%m-%d %H:%M} → {leg.arrival:%m-%d %H:%M}</span>'
            f'<strong class="trip-route">{escape(leg.origin_name)} → {escape(leg.destination_name)}</strong>'
            f'<span class="trip-service">{escape(MODE_NAMES[leg.mode.value])} · ¥{leg.cost_cents / 100:.2f}</span></li>'
            for leg in plan.legs
        )
        activities = "".join(
            f'<div class="trip-activity">{escape(a.label)} · {escape(a.location)} {a.start:%m-%d %H:%M}—{a.end:%m-%d %H:%M}</div>'
            for a in plan.activities
        )
        status = (
            "演示数据 · 非实际可预订路线" if any(leg.demo for leg in plan.legs) else "请出发前核实班次与费用"
        )
        cards.append(
            '<article class="trip-card">'
            f'<div class="trip-card-heading"><h3>{escape(plan.label)}方案</h3><span class="trip-badge">{index + 1:02d}</span></div>'
            f'<div class="trip-card-status">{status}</div>'
            + metrics(plan.total_cost_cents, plan.total_minutes, plan.transfers)
            + f'<details {"open" if index == 0 else ""}><summary>行程时间轴</summary><ol class="trip-timeline">{legs}</ol>{activities}</details>'
            + notes_detail(plan.risks)
            + "</article>"
        )
    return '<section class="trip-card-grid" aria-label="方案对比">' + "".join(cards) + "</section>"


def render_intelligent(value: dict | None) -> str:
    """Retain incomplete-cost and partial-itinerary warnings alongside compact summaries."""
    if not value:
        return ""
    report = PlanningReport.model_validate(value)
    if not report.options:
        return notice(
            "再补充一点，就能继续" if report.questions else "暂未找到合适的路线",
            report.questions or report.data_gaps or ["可在对话中补充时间、调整预算或交通方式后继续规划。"],
        )
    parts = []
    for index, option in enumerate(display_options(report)):
        complete = report.completed_stops == report.total_stops
        status = (
            "全程候选 · 班次、票价与余票待核验"
            if complete
            else f"已完成 {report.completed_stops}/{report.total_stops} 站 · 后续待解决"
        )
        title = f"路线 {index + 1:02d}"
        if report.metro_then_taxi and option.routes:
            taxi = next((s for s in reversed(option.routes[-1].steps) if s.mode.value == "taxi"), None)
            if taxi:
                title = f"{taxi.origin}下车，再打车"
                status = "已检查地铁末班衔接 · 打车价格为估算"
        timeline = []
        for route_index, route in enumerate(option.routes):
            services = " → ".join(s.name for s in route.steps if s.mode.value != "walk") or "步行"
            activity = option.activities[route_index] if route_index < len(option.activities) else None
            activity_html = (
                f'<div class="trip-activity">{escape(activity.label)} · {activity.start:%m-%d %H:%M}—{activity.end:%m-%d %H:%M}</div>'
                if activity and activity.end > activity.start
                else ""
            )
            timeline.append(
                f'<li><span class="trip-time">{route.departure:%m-%d %H:%M} → {route.arrival:%m-%d %H:%M}</span>'
                f'<strong class="trip-route">{escape(route.origin)} → {escape(route.destination)}</strong>'
                f'<span class="trip-service">{escape(services)}</span>{activity_html}</li>'
            )
        minutes = (option.ready - option.routes[0].departure).total_seconds() / 60 if option.routes else 0
        rides = [s for r in option.routes for s in r.steps if s.mode.value not in {"walk", "shared_bike"}]
        warning = (
            '<div class="trip-warning">另有未核实费用，暂不能确认满足预算。</div>'
            if option.unknown_cost
            else ""
        )
        if not complete:
            warning += '<div class="trip-warning">以下为已算出的部分行程，不是完整可执行方案。</div>'
        notes = [*option.decisions, *report.assumptions, *report.data_gaps]
        notes += [w for route in option.routes for w in route.warnings]
        parts.append(
            '<article class="trip-card">'
            f'<div class="trip-card-heading"><h3>{escape(title)}</h3><span class="trip-badge">候选 {index + 1}</span></div>'
            f'<div class="trip-card-status">{escape(status)}</div>'
            + metrics(option.transport_cents, minutes, max(0, len(rides) - 1), estimate=option.unknown_cost)
            + warning
            + f'<details {"open" if index == 0 else ""}><summary>行程时间轴</summary><ol class="trip-timeline">{"".join(timeline)}</ol></details>'
            + notes_detail(notes)
            + "</article>"
        )
    return '<section class="trip-card-grid" aria-label="智能规划对比">' + "".join(parts) + "</section>"
