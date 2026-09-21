"""Time-window multi-label Dijkstra enumeration on a bounded subgraph."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from heapq import heappop, heappush
from itertools import count
from math import ceil
from time import monotonic

from src.domain import Constraints, Edge, Leg, Mode, Network, Plan
from src.errors import DeadlineExceeded


def transfer_buffer(previous: Leg | None, edge: Edge, default: int) -> int:
    if previous and previous.service_id == edge.service_id:
        return 0
    if edge.mode == Mode.flight:
        return max(default, 90)
    return default if previous else 0


def departure(edge: Edge, ready: datetime) -> datetime | None:
    """Find a boardable departure; the last train may arrive after operating_end."""
    if edge.seats == 0:
        return None
    if edge.departures:
        return min((d for d in edge.departures if d >= ready), default=None)
    start, end = edge.operating_start, edge.operating_end
    if start is None or end is None:
        return None
    value = max(ready, start)
    if edge.headway_min:
        steps = ceil((value - start).total_seconds() / (60 * edge.headway_min))
        value = start + timedelta(minutes=steps * edge.headway_min)
    return value if value <= end else None


def transfers(legs: list[Leg]) -> int:
    services = [leg.service_id for leg in legs if leg.mode != Mode.walk]
    return sum(a != b for a, b in zip(services, services[1:]))


def permitted(edge: Edge, c: Constraints) -> bool:
    return edge.mode not in c.excluded_modes and not (
        edge.mode == Mode.shared_bike
        and (c.cycling_acceptance == 0 or (c.cycling_acceptance == 1 and edge.distance_m > 3000))
    )


@dataclass
class SearchStats:
    expansions: int = 0
    truncated: bool = False
    limit: int = 15000
    deadline: float = float("inf")
    night_branches: int = 0

    def check(self) -> bool:
        if monotonic() >= self.deadline:
            raise DeadlineExceeded()
        if self.expansions >= self.limit:
            self.truncated = True
            return False
        self.expansions += 1
        return True


@dataclass
class TransitGraph:
    network: Network
    modes: set[Mode]
    nodes: dict = field(init=False)
    adjacency: dict = field(init=False)

    def __post_init__(self) -> None:
        self.nodes = {n.id: n for n in self.network.nodes}
        self.adjacency = {n.id: [] for n in self.network.nodes}
        for edge in self.network.edges:
            if edge.mode in self.modes:
                self.adjacency[edge.origin].append(edge)


def search(
    graph: TransitGraph,
    origin: str,
    destination: str,
    c: Constraints,
    ready: datetime,
    k: int = 3,
    objective: str = "cost",
    prefix: list[Leg] | None = None,
    buffer_min: int = 15,
    stats: SearchStats | None = None,
    max_hops: int = 8,
) -> list[list[Leg]]:
    """Enumerate simple paths with time and cost labels; no single visited[node] pruning.

    Nonnegative costs/durations justify heap ordering. Prefix cost/resources participate
    in pruning. Top-K and shared expansion bounds make this an approximate planner.
    """
    prefix = prefix or []
    stats = stats or SearchStats()
    if origin == destination:
        return [[]]
    serial = count()
    queue: list = [(0, next(serial), origin, ready, [], frozenset([origin]))]
    results: list[list[Leg]] = []
    while queue and len(results) < k:
        if not stats.check():
            break
        _, _, node, at, path, visited = heappop(queue)
        if node == destination:
            results.append(path)
            continue
        if len(path) >= max_hops:
            stats.truncated = True
            continue
        previous = (prefix + path)[-1] if prefix or path else None
        for edge in graph.adjacency.get(node, []):
            if edge.destination in visited or not permitted(edge, c):
                continue
            dep = departure(edge, at + timedelta(minutes=transfer_buffer(previous, edge, buffer_min)))
            if dep is None:
                continue
            arr = dep + timedelta(minutes=edge.duration_min)
            if arr > c.arrive_by or (not prefix and not path and dep > c.depart_before):
                continue
            last = max(edge.departures) if edge.departures else edge.operating_end
            leg = Leg(
                edge_id=edge.id,
                origin=edge.origin,
                destination=edge.destination,
                origin_name=graph.nodes[edge.origin].name,
                destination_name=graph.nodes[edge.destination].name,
                mode=edge.mode,
                service_id=edge.service_id,
                departure=dep,
                arrival=arr,
                cost_cents=edge.cost_cents,
                distance_m=edge.distance_m,
                source=edge.source,
                observed_at=edge.observed_at,
                demo=edge.demo,
                official=edge.official,
                seats=edge.seats,
                fare_estimated=edge.fare_estimated,
                last_service=bool(
                    last and 0 <= (last - dep).total_seconds() < max(600, edge.headway_min * 60)
                ),
            )
            whole = prefix + path + [leg]
            cost = sum(x.cost_cents for x in whole)
            if c.budget_cents is not None and cost > c.budget_cents:
                continue
            if sum(x.distance_m for x in whole if x.mode == Mode.walk) > c.max_walk_m:
                continue
            if sum(x.distance_m for x in whole if x.mode == Mode.shared_bike) > c.max_bike_m:
                continue
            key = (
                (cost, arr.timestamp())
                if objective == "cost"
                else (
                    (transfers(whole), arr.timestamp(), cost)
                    if objective == "transfers"
                    else (arr.timestamp(), cost)
                )
            )
            heappush(
                queue, (key, next(serial), edge.destination, arr, path + [leg], visited | {edge.destination})
            )
    if queue:
        stats.truncated = True
    return results


def make_plan(legs: list[Leg], c: Constraints, edges: dict[str, Edge], buffer_min: int = 15) -> Plan | None:
    """Final authoritative hard-constraint gate, used also for degraded results."""
    if not legs or legs[0].departure < c.depart_after or legs[0].departure > c.depart_before:
        return None
    if legs[-1].arrival > c.arrive_by:
        return None
    for i, leg in enumerate(legs):
        edge = edges.get(leg.edge_id)
        if not edge or not permitted(edge, c):
            return None
        if (leg.origin, leg.destination, leg.mode, leg.service_id, leg.cost_cents, leg.distance_m) != (
            edge.origin,
            edge.destination,
            edge.mode,
            edge.service_id,
            edge.cost_cents,
            edge.distance_m,
        ):
            return None
        if departure(edge, leg.departure) != leg.departure:
            return None
        if leg.arrival != leg.departure + timedelta(minutes=edge.duration_min):
            return None
        if i:
            prev = legs[i - 1]
            if prev.destination != leg.origin or leg.departure < prev.arrival + timedelta(
                minutes=transfer_buffer(prev, edge, buffer_min)
            ):
                return None
    cost = sum(x.cost_cents for x in legs)
    if c.budget_cents is not None and cost > c.budget_cents:
        return None
    if sum(x.distance_m for x in legs if x.mode == Mode.walk) > c.max_walk_m:
        return None
    if sum(x.distance_m for x in legs if x.mode == Mode.shared_bike) > c.max_bike_m:
        return None
    if c.max_transfers is not None and transfers(legs) > c.max_transfers:
        return None
    from hashlib import sha256

    identity = "|".join(x.edge_id + x.departure.isoformat() for x in legs)
    return Plan(
        id=sha256(identity.encode()).hexdigest()[:16],
        legs=legs,
        total_cost_cents=cost,
        total_minutes=(legs[-1].arrival - legs[0].departure).total_seconds() / 60,
        transfers=transfers(legs),
    )
