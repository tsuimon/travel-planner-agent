"""Algorithmic regressions: feasible labels, boardability and exact cost/time accounting."""

from datetime import timedelta
import pytest
from src.domain import Edge, Mode, Network, Node
from src.search.algorithm import SearchStats, TransitGraph, departure, make_plan, search
from src.search.planner import HierarchicalPlanner, rank
from src.search.sample import sample_network


def test_pareto_early_expensive_label_must_survive(night):
    start = night.depart_after
    nodes = [Node(id=x, name=x, city="test") for x in ("s", "m", "t")]

    def edge(name, a, b, price, duration, minutes):
        return Edge(
            id=name,
            origin=a,
            destination=b,
            service_id=name,
            mode=Mode.bus,
            cost_cents=price,
            duration_min=duration,
            departures=[start + timedelta(minutes=minutes)],
            observed_at=start,
            source="test",
        )

    network = Network(
        nodes=nodes,
        edges=[
            edge("slow", "s", "m", 100, 40, 0),
            edge("fast", "s", "m", 500, 5, 0),
            edge("only", "m", "t", 100, 5, 25),
        ],
        coverage="test",
    )
    paths = search(TransitGraph(network, {Mode.bus}), "s", "t", night, start)
    assert [[x.edge_id for x in path] for path in paths] == [["fast", "only"]]


def test_cross_midnight_and_last_departure(night):
    start = night.depart_after.replace(hour=23, minute=50)
    edge = Edge(
        id="cross",
        origin="a",
        destination="b",
        service_id="x",
        mode=Mode.bus,
        cost_cents=100,
        duration_min=30,
        operating_start=start,
        operating_end=start + timedelta(minutes=20),
        headway_min=10,
        observed_at=start,
        source="test",
    )
    assert departure(edge, start + timedelta(minutes=15)) == start + timedelta(minutes=20)
    assert departure(edge, start + timedelta(minutes=21)) is None
    assert departure(edge.model_copy(update={"seats": 0}), start) is None


def test_all_ranked_routes_obey_constraints(night):
    network = sample_network(night.depart_after)
    routes = HierarchicalPlanner().plan(network, night, "cost", SearchStats())
    selected = rank(routes, night)
    assert 2 <= len(selected) <= 3
    assert any(Mode.metro in [x.mode for x in p.legs] for p in selected)
    assert any([x.mode for x in p.legs] == [Mode.taxi] for p in selected)
    for plan in selected:
        assert plan.total_cost_cents == sum(x.cost_cents for x in plan.legs)
        assert plan.total_minutes == (plan.legs[-1].arrival - plan.legs[0].departure).total_seconds() / 60
        assert make_plan(plan.legs, night, {e.id: e for e in network.edges}) is not None


def test_last_train_missed_never_boarded(night):
    c = night.model_copy(
        update={
            "depart_after": night.depart_after.replace(hour=23, minute=20),
            "depart_before": night.depart_before.replace(hour=23, minute=40),
        }
    )
    results = HierarchicalPlanner().plan(sample_network(c.depart_after), c, "cost", SearchStats())
    assert results
    assert all(Mode.metro not in [leg.mode for leg in p.legs] for p in results)


def test_exclusions_and_total_bike_distance(night):
    c = night.model_copy(update={"excluded_modes": [Mode.taxi, Mode.walk], "max_bike_m": 1000})
    assert HierarchicalPlanner().plan(sample_network(c.depart_after), c, "cost", SearchStats()) == []


def test_truncated_search_is_observable(night):
    stats = SearchStats(limit=1)
    HierarchicalPlanner().plan(sample_network(night.depart_after), night, "cost", stats)
    assert stats.truncated and stats.expansions == 1


def test_naive_schedule_is_rejected(night):
    with pytest.raises(ValueError):
        Edge(
            id="x",
            origin="a",
            destination="b",
            mode=Mode.metro,
            service_id="x",
            cost_cents=1,
            duration_min=10,
            departures=[night.depart_after.replace(tzinfo=None)],
            source="test",
            observed_at=night.depart_after,
        )


def test_final_validator_rejects_modified_price(night):
    network = sample_network(night.depart_after)
    plan = HierarchicalPlanner().plan(network, night, "cost", SearchStats())[0]
    altered = [leg.model_copy() for leg in plan.legs]
    altered[0].cost_cents = 0
    assert make_plan(altered, night, {e.id: e for e in network.edges}) is None
