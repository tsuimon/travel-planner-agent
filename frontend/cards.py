"""Escaped, compact plan cards for the Gradio results pane."""

from html import escape
from src.domain import Plan
from src.risk import MODE_NAMES
from src.agent.intelligent import PlanningReport, display_options


def render_cards(plans: list[Plan]) -> str:
    if not plans:
        return ""
    cards = []
    for plan in plans:
        legs = "".join(
            f'<li style="margin:10px 0"><strong>{leg.departure:%H:%M} → {leg.arrival:%H:%M}</strong>'
            f"<br>{escape(leg.origin_name)} → {escape(leg.destination_name)}"
            f"<br><small>{escape(MODE_NAMES[leg.mode.value])} · ¥{leg.cost_cents / 100:.2f}</small></li>"
            for leg in plan.legs
        )
        risks = "".join(f"<li>{escape(r)}</li>" for r in plan.risks)
        activities = "".join(
            f"<li>{a.start:%m-%d %H:%M}—{a.end:%H:%M} {escape(a.location)} · {escape(a.label)}</li>"
            for a in plan.activities
        )
        cards.append(
            f'<article style="flex:1;min-width:260px;border:1px solid #cbd5e1;border-radius:14px;'
            f'padding:20px;background:#f8fafc;color:#172033">'
            f'<h3 style="margin:0;color:#0f766e">{escape(plan.label)}</h3>'
            f'<p style="font-size:28px;font-weight:700;margin:12px 0">¥{plan.total_cost_cents / 100:.2f}</p>'
            f"<p>{plan.total_minutes:g} 分钟 · {plan.transfers} 次换乘</p>"
            f'<ol style="padding-left:20px">{legs}</ol>'
            f'<ul style="padding-left:20px">{activities}</ul>'
            f'<details><summary>数据与执行风险</summary><ul style="padding-left:20px">{risks}</ul></details>'
            f"</article>"
        )
    return (
        '<section aria-label="方案对比" style="display:flex;gap:16px;flex-wrap:wrap">'
        + "".join(cards)
        + "</section>"
    )


def render_intelligent(value: dict | None) -> str:
    """Show whole-trip progress and active choices, not empty cards for partial results."""
    if not value:
        return ""
    report = PlanningReport.model_validate(value)
    if not report.options:
        return ""
    parts = []
    for index, option in enumerate(display_options(report)):
        status = (
            "全程候选 · 票价与余票待核验"
            if report.completed_stops == report.total_stops
            else f"已算出前{report.completed_stops}/{report.total_stops}站 · 后续待解决"
        )
        title = f"候选{index + 1}"
        if report.metro_then_taxi:
            taxi = next(s for s in reversed(option.routes[-1].steps) if s.mode.value == "taxi")
            title = f"推荐：{taxi.origin}下车再打车"
            status = "已检查地铁末班衔接 · 打车价格为估算"
        timeline = []
        for route, activity in zip(option.routes, option.activities):
            services = " → ".join(s.name for s in route.steps if s.mode.value != "walk") or "步行"
            timeline.append(
                f"<li><strong>{escape(route.origin)} → {escape(route.destination)}</strong>"
                f"<br>{route.departure:%m-%d %H:%M}—{route.arrival:%m-%d %H:%M}"
                f"<br>{escape(services)}<br>{escape(activity.label)}至 {activity.end:%m-%d %H:%M}</li>"
            )
        notes = "；".join(dict.fromkeys(option.decisions))
        incomplete = "（费用不完整）" if option.unknown_cost else ""
        parts.append(
            f'<article style="flex:1;min-width:280px;padding:20px;border:1px solid #b8d9d1;border-radius:14px;background:#f5faf8;color:#163a32">'
            f"<h3>{escape(title)} · 交通参考¥{option.transport_cents / 100:.2f}{incomplete}</h3>"
            f"<p>{status}</p><ol>{''.join(timeline)}</ol><p>{escape(notes)}</p></article>"
        )
    return (
        '<section aria-label="智能规划对比" style="display:flex;gap:16px;flex-wrap:wrap">'
        + "".join(parts)
        + "</section>"
    )
