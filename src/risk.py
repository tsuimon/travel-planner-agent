"""Evidence-derived labels; never claim alternative availability without data."""

from src.domain import Mode, Plan, Weather
from src.search.planner import is_night


def annotate(plan: Plan, weather: Weather, cached: bool = False) -> Plan:
    labels: list[str] = []
    if any(x.demo for x in plan.legs):
        labels.append("演示数据：班次、价格及库存均非真实出行依据")
    if any(not x.official for x in plan.legs):
        labels.append("非官方数据，仅供参考")
    if any(
        x.seats is None and x.mode not in {Mode.walk, Mode.metro, Mode.bus, Mode.taxi, Mode.shared_bike}
        for x in plan.legs
    ):
        labels.append("余票未知，需确认可预订性")
    if cached:
        labels.append("使用缓存数据，出发前需重新确认")
    if any(x.fare_estimated for x in plan.legs):
        labels.append("含估算费用，无法保证实际支出不超过预算")
    if any(x.mode == Mode.shared_bike for x in plan.legs):
        labels.append("共享单车库存和停放区未核实")
        if any(x.mode == Mode.shared_bike and is_night(x.departure) for x in plan.legs):
            labels.append("夜间覆盖不确定")
        if weather.rain_probability is not None and weather.rain_probability >= 0.4:
            labels.append("骑行可能受影响" + ("（演示天气）" if weather.demo else "（有雨）"))
    if any(x.last_service and x.mode in {Mode.metro, Mode.bus} for x in plan.legs):
        labels.append("赶末班车：错过需重新规划，可能需打车；不保证替代车辆可用")
    if weather.rain_probability is None:
        labels.append("天气信息不可用，未完成天气风险核验")
    plan.risks = list(dict.fromkeys(labels))
    return plan


MODE_NAMES = {
    "high_speed_rail": "高铁",
    "normal_rail": "普速",
    "flight": "飞机",
    "coach": "大巴",
    "county_bus": "县域班车",
    "ferry": "轮渡",
    "metro": "地铁",
    "bus": "公交",
    "taxi": "网约车",
    "shared_bike": "共享单车",
    "walk": "步行",
}


def describe(plans: list[Plan]) -> str:
    parts = ["以下是在当前数据覆盖内找到的候选方案；请留意每套方案的数据与执行风险。"]
    for p in plans:
        parts.append(
            f"**{p.label}**：¥{p.total_cost_cents / 100:.2f}，{p.total_minutes:g}分钟，换乘{p.transfers}次。"
        )
        for leg in p.legs:
            parts.append(
                f"- {leg.departure:%m-%d %H:%M}—{leg.arrival:%m-%d %H:%M} "
                f"{leg.origin_name} → {leg.destination_name}，{MODE_NAMES[leg.mode.value]} "
                f"({leg.service_id})，¥{leg.cost_cents / 100:.2f}"
            )
        for activity in p.activities:
            if activity.start != activity.end:
                parts.append(
                    f"- 活动 {activity.start:%m-%d %H:%M}—{activity.end:%m-%d %H:%M}："
                    f"{activity.location}，{activity.label}"
                )
        parts.append("风险：" + "；".join(p.risks))
    if len(plans) > 1:
        parts.append(
            f"候选费用相差¥{(max(p.total_cost_cents for p in plans) - min(p.total_cost_cents for p in plans)) / 100:.2f}；"
            f"耗时相差{max(p.total_minutes for p in plans) - min(p.total_minutes for p in plans):g}分钟。"
        )
    return "\n\n".join(parts)
