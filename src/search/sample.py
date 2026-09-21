"""Synthetic schedules only: never use these data to make an actual journey."""

from datetime import datetime, timedelta
from src.domain import Edge, Mode, Network, Node, TZ


def sample_network(start: datetime) -> Network:
    day = start.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    nodes = [
        Node(id=i, name=name, city=city, hub=hub)
        for i, name, city, hub in [
            ("haidian", "北京海淀", "北京", False),
            ("beijing", "北京南站", "北京", True),
            ("tj", "天津站", "天津", True),
            ("binhai", "天津滨海新区", "天津", False),
            ("night_start", "夜间起点", "示例城", False),
            ("last_stop", "末班可达站", "示例城", False),
            ("night_end", "夜间终点", "示例城", False),
            ("county_a", "甲县", "甲县", True),
            ("hub_a", "甲枢纽", "甲城", True),
            ("hub_b", "乙枢纽", "乙城", True),
            ("county_b", "乙县", "乙县", True),
            ("shanghai", "上海虹桥站", "上海", True),
            ("island", "示例岛", "示例岛", True),
            ("airport", "示例机场", "示例机场", True),
        ]
    ]
    edges: list[Edge] = []

    def add(
        eid: str,
        a: str,
        b: str,
        mode: Mode,
        duration: int,
        cost: int,
        clocks: list[int] | None = None,
        first: int = 0,
        last: int = 2880,
        headway: int = 0,
        distance: int = 0,
    ) -> None:
        edges.append(
            Edge(
                id=eid,
                origin=a,
                destination=b,
                mode=mode,
                service_id=eid,
                duration_min=duration,
                cost_cents=cost,
                distance_m=distance,
                departures=[day + timedelta(minutes=c) for c in (clocks or [])],
                operating_start=day + timedelta(minutes=first),
                operating_end=day + timedelta(minutes=last),
                headway_min=headway,
                observed_at=day,
                source="synthetic://v1/" + eid,
                demo=True,
                fare_estimated=mode == Mode.taxi,
            )
        )

    add("BJ-M", "haidian", "beijing", Mode.metro, 35, 500, first=360, last=1380, headway=10)
    add("BJ-B", "haidian", "beijing", Mode.bus, 60, 200, first=360, last=1380, headway=20)
    add("BJ-T", "haidian", "beijing", Mode.taxi, 28, 5500, distance=18000)
    add("DEMO-G", "beijing", "tj", Mode.high_speed_rail, 40, 5450, [1140, 1200, 1320, 1390, 1500])
    add("DEMO-K", "beijing", "tj", Mode.normal_rail, 100, 2350, [1150, 1260, 1370])
    add("DEMO-COACH", "beijing", "tj", Mode.coach, 140, 3000, [1170, 1300])
    add("TJ-M", "tj", "binhai", Mode.metro, 50, 700, first=360, last=1360, headway=10)
    add("TJ-B", "tj", "binhai", Mode.bus, 85, 300, first=360, last=1410, headway=15)
    add("TJ-T", "tj", "binhai", Mode.taxi, 40, 10000, distance=45000)
    add("N-LAST", "night_start", "last_stop", Mode.metro, 20, 400, [1320, 1350, 1360, 1380])
    add("N-CLOSED", "last_stop", "night_end", Mode.metro, 12, 300, [1320, 1340])
    add("N-TAXI", "last_stop", "night_end", Mode.taxi, 8, 1800, distance=2500)
    add("N-BIKE", "last_stop", "night_end", Mode.shared_bike, 16, 150, distance=2500)
    add("N-WALK", "last_stop", "night_end", Mode.walk, 35, 0, distance=2500)
    add("N-DIRECT", "night_start", "night_end", Mode.taxi, 32, 8000, distance=18000)
    add("COUNTY-A", "county_a", "hub_a", Mode.county_bus, 40, 1000, [480, 1080])
    add("COUNTY-G", "hub_a", "hub_b", Mode.high_speed_rail, 60, 3000, [540, 1140])
    add("COUNTY-B", "hub_b", "county_b", Mode.county_bus, 45, 1000, [630, 1230])
    add("DEMO-SH", "beijing", "shanghai", Mode.high_speed_rail, 330, 55300, [600, 1140])
    add("DEMO-FERRY", "hub_b", "island", Mode.ferry, 40, 2500, [650, 1250])
    add("DEMO-FLIGHT", "beijing", "airport", Mode.flight, 120, 60000, [720, 1260])
    return Network(nodes=nodes, edges=edges, coverage="合成演示：京津、夜间示例城、甲乙县；非真实交通数据")
