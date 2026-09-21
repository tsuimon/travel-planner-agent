"""Hierarchical routing, explicit night-tail exploration, and diverse ranking."""

from datetime import datetime
from src.domain import Constraints, LOCAL, PUBLIC, Mode, Network, Plan, TZ
from src.search.algorithm import SearchStats, TransitGraph, make_plan, search


def is_night(at: datetime) -> bool:
    hour = at.astimezone(TZ).hour
    return hour >= 22 or hour < 6


def local_search(
    network: Network,
    a: str,
    b: str,
    c: Constraints,
    ready: datetime,
    prefix: list,
    objective: str,
    buffer_min: int,
    stats: SearchStats,
) -> list[list]:
    args = dict(c=c, ready=ready, prefix=prefix, objective=objective, buffer_min=buffer_min, stats=stats)
    paths = search(TransitGraph(network, LOCAL), a, b, **args)
    if not is_night(ready):
        return paths
    stats.night_branches += 1
    # Night-specific branch: every boardable public-transport terminal is a candidate.
    public = TransitGraph(network, PUBLIC)
    tails = TransitGraph(network, {Mode.taxi, Mode.shared_bike, Mode.walk})
    terminals = sorted({e.destination for e in network.edges if e.mode in PUBLIC})
    for stop in terminals:
        if stop in (a, b):
            continue
        for head in search(public, a, stop, **args, k=2):
            for tail in search(
                tails,
                stop,
                b,
                c,
                head[-1].arrival,
                prefix=prefix + head,
                objective=objective,
                buffer_min=buffer_min,
                stats=stats,
            ):
                paths.append(head + tail)
    # Preserve the convenient comparison only if all hard constraints allow it.
    paths += search(TransitGraph(network, {Mode.taxi}), a, b, **args, k=1)
    return paths


def rank(plans: list[Plan], c: Constraints) -> list[Plan]:
    unique = {p.id: p for p in plans}
    values = list(unique.values())
    if not values:
        return []
    max_cost = max(p.total_cost_cents for p in values) or 1
    max_time = max(p.total_minutes for p in values) or 1
    for p in values:
        bike = sum(x.mode == Mode.shared_bike for x in p.legs)
        discomfort = sum(x.mode in {Mode.walk, Mode.shared_bike, Mode.bus, Mode.county_bus} for x in p.legs)
        p.score = round(
            (0.55 if c.budget_preference == "economy" else 0.35) * p.total_cost_cents / max_cost
            + (0.5 if c.budget_preference == "fast" else 0.25) * p.total_minutes / max_time
            + 0.15 * max(0, p.transfers - c.transfer_tolerance)
            + 0.1 * bike / (c.cycling_acceptance + 1)
            + (0.15 if c.comfort_priority == "high" else 0.05) * discomfort,
            4,
        )
    chosen: list[Plan] = []
    goals = [
        ("省钱", lambda p: (p.total_cost_cents, p.total_minutes)),
        ("省时", lambda p: (p.total_minutes, p.total_cost_cents)),
        ("少换乘", lambda p: (p.transfers, p.total_minutes, p.total_cost_cents)),
    ]
    for label, key in goals:
        best = min(values, key=key)
        if best not in chosen:
            best.label = label
            chosen.append(best)
        else:
            best.label += " / " + label
    for p in sorted(values, key=lambda p: p.score):
        if len(chosen) >= 3:
            break
        if p not in chosen:
            p.label = "备选"
            chosen.append(p)
    return sorted(chosen, key=lambda p: p.score)


class HierarchicalPlanner:
    def __init__(self, buffer_min: int = 15) -> None:
        self.buffer_min = buffer_min

    def plan(self, network: Network, c: Constraints, objective: str, stats: SearchStats) -> list[Plan]:
        nodes = {n.id: n for n in network.nodes}
        if c.origin not in nodes or c.destination not in nodes:
            return []
        a, b = nodes[c.origin], nodes[c.destination]
        paths: list[list] = []
        if a.city == b.city:
            paths = local_search(
                network, a.id, b.id, c, c.depart_after, [], objective, self.buffer_min, stats
            )
        else:
            local = TransitGraph(network, LOCAL)
            trunk = TransitGraph(network, set(Mode) - LOCAL)
            origin_hubs = [n.id for n in network.nodes if n.city == a.city and n.hub]
            dest_hubs = [n.id for n in network.nodes if n.city == b.city and n.hub]
            for start in origin_hubs:
                for access in search(
                    local,
                    a.id,
                    start,
                    c,
                    c.depart_after,
                    prefix=[],
                    objective=objective,
                    buffer_min=self.buffer_min,
                    stats=stats,
                ):
                    ready = access[-1].arrival if access else c.depart_after
                    for end in dest_hubs:
                        for main in search(
                            trunk,
                            start,
                            end,
                            c,
                            ready,
                            k=5,
                            prefix=access,
                            objective=objective,
                            buffer_min=self.buffer_min,
                            stats=stats,
                        ):
                            if not main:
                                continue
                            for exit_path in local_search(
                                network,
                                end,
                                b.id,
                                c,
                                main[-1].arrival,
                                access + main,
                                objective,
                                self.buffer_min,
                                stats,
                            ):
                                paths.append(access + main + exit_path)
        edges = {e.id: e for e in network.edges}
        results = [make_plan(path, c, edges, self.buffer_min) for path in paths]
        return [p for p in results if p is not None]
