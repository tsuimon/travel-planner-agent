"""Presentation must retain planning caveats and escape provider/user content."""

from frontend.cards import render_intelligent, notice
from src.agent.intelligent import PlanningReport, JourneyOption


def test_unknown_cost_and_partial_journey_stay_visible(now):
    report = PlanningReport(
        completed_stops=1,
        total_stops=3,
        options=[JourneyOption(location="A", ready=now, transport_cents=500, unknown_cost=True)],
    )
    html = render_intelligent(report.model_dump(mode="json"))
    assert "已知交通费用" in html and "暂不能确认满足预算" in html
    assert "不是完整可执行方案" in html and "1/3" in html


def test_missing_routes_do_not_crash_night_card(now):
    report = PlanningReport(metro_then_taxi=True, options=[JourneyOption(location="A", ready=now)])
    assert "路线 01" in render_intelligent(report.model_dump(mode="json"))


def test_clarification_is_visible_and_provider_text_is_escaped():
    report = PlanningReport(questions=["<img src=x onerror=alert(1)>几点到？"])
    html = render_intelligent(report.model_dump(mode="json"))
    assert "再补充一点" in html and "&lt;img" in html and "<img" not in html
    assert "&lt;script&gt;" in notice("<script>", ["<script>"])
