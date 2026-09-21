"""Real-provider route evidence, kept separate from fully verified ticket plans."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pydantic import Field

from src.domain import Model, Mode, TZ


class RouteStep(Model):
    mode: Mode
    name: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    distance_m: int = 0
    scheduled: bool = False
    last_boarding: datetime | None = None


class RouteEvidence(Model):
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    cost_cents: int | None = None
    steps: list[RouteStep]
    source: str = "https://restapi.amap.com/v5/direction/transit/integrated"
    queried_at: datetime
    warnings: list[str] = Field(default_factory=list)
    cost_incomplete: bool = False


def cents(value) -> int | None:
    try:
        amount = Decimal(str(value))
        return int(amount * 100) if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def clock_on(day: datetime, clock: str) -> datetime:
    raw = str(clock).replace(":", "")
    if len(raw) != 4 or not raw.isdigit():
        raise ValueError("missing_service_clock")
    return day.replace(hour=int(raw[:2]), minute=int(raw[2:]), second=0, microsecond=0)


def inside_service(at: datetime, first: str, last: str) -> bool:
    if not first or not last:
        return True  # Returned as unverified below, never a proven operating window.
    start, end = clock_on(at, first), clock_on(at, last)
    if end < start:
        return at >= start or at <= end
    return start <= at <= end


def ordered_segments(segments: list[dict]):
    """AMap can put a feeder taxi and its train into the same object; order by endpoints."""
    for segment in segments:
        taxi, rail = segment.get("taxi"), segment.get("railway")
        if not taxi or not rail:
            yield segment
            continue
        departure, arrival = rail.get("departure_stop") or {}, rail.get("arrival_stop") or {}

        def coords(value):
            return str(value or "").replace(" ", ",")

        before = (taxi.get("endname") and taxi["endname"] == departure.get("name")) or (
            taxi.get("endpoint") and coords(taxi["endpoint"]) == coords(departure.get("location"))
        )
        after = (taxi.get("startname") and taxi["startname"] == arrival.get("name")) or (
            taxi.get("startpoint") and coords(taxi["startpoint"]) == coords(arrival.get("location"))
        )
        rest = {k: v for k, v in segment.items() if k != "taxi"}
        if before:
            yield {"taxi": taxi}
            yield rest
        elif after:
            yield rest
            yield {"taxi": taxi}
        else:
            raise ValueError("ambiguous_taxi_train_connection")


def normalize_transit(
    body: dict, origin: str, destination: str, at: datetime
) -> tuple[list[RouteEvidence], list[str]]:
    """Rebuild arrival times from actual train clocks; do not trust aggregate duration."""
    if body.get("status") != "1" or not isinstance(body.get("route"), dict):
        raise ValueError("invalid_route_response")
    routes, rejected = [], []
    for raw in body["route"].get("transits", [])[:5]:
        cursor, steps = at, []
        cost_incomplete = False
        warnings = [
            "高德路线参考；铁路服务日期、票价和余票尚未向售票方核验",
            "地铁/步行时刻按耗时推算，铁路进站预留30分钟",
        ]
        try:
            for segment in ordered_segments(raw.get("segments", [])):
                walking = segment.get("walking") or {}
                seconds = int((walking.get("cost") or {}).get("duration") or 0)
                if seconds:
                    end = cursor + timedelta(seconds=seconds)
                    steps.append(
                        RouteStep(
                            mode=Mode.walk,
                            name=f"步行{walking.get('distance', '?')}米",
                            origin=steps[-1].destination if steps else origin,
                            destination="接续站点/目的地",
                            departure=cursor,
                            arrival=end,
                            distance_m=int(walking.get("distance") or 0),
                        )
                    )
                    cursor = end
                rail = segment.get("railway") or {}
                if rail:
                    dep, arr = rail["departure_stop"], rail["arrival_stop"]
                    departure = clock_on(at, dep.get("time", ""))
                    arrival = departure + timedelta(seconds=int(rail["time"]))
                    reported = clock_on(arrival, arr.get("time", ""))
                    if abs((arrival - reported).total_seconds()) > 60:
                        raise ValueError("rail_clock_conflict")
                    if departure < cursor + timedelta(minutes=30):
                        raise ValueError("rail_connection_too_short")
                    mode = (
                        Mode.high_speed_rail if str(rail.get("trip", ""))[:1] in "GDC" else Mode.normal_rail
                    )
                    steps.append(
                        RouteStep(
                            mode=mode,
                            name=rail["trip"],
                            origin=dep["name"],
                            destination=arr["name"],
                            departure=departure,
                            arrival=arrival,
                            scheduled=True,
                            distance_m=int(rail.get("distance") or 0),
                        )
                    )
                    cursor = arrival + timedelta(minutes=10)  # station exit buffer
                lines = (segment.get("bus") or {}).get("buslines", [])
                if lines:
                    line = lines[0]  # alternatives are not consecutive rides
                    mode = Mode.metro if "地铁" in line.get("type", "") else Mode.bus
                    cursor += timedelta(minutes=5)
                    first = line.get("station_start_time") or line.get("start_time")
                    last = line.get("station_end_time") or line.get("end_time")
                    if first and last:
                        opening, closing = clock_on(cursor, first), clock_on(cursor, last)
                        if opening <= closing and cursor < opening:
                            cursor = (
                                opening  # wait for first service rather than reject a useful early departure
                            )
                    if not inside_service(cursor, first, last):
                        raise ValueError("outside_operating_hours")
                    if not first or not last:
                        warnings.append("部分公交首末班缺失，运营时间需确认")
                    seconds = int((line.get("cost") or {}).get("duration") or 0)
                    if seconds <= 0:
                        raise ValueError("missing_bus_duration")
                    end = cursor + timedelta(seconds=seconds)
                    last_boarding = clock_on(cursor, last) if line.get("station_end_time") else None
                    if (
                        last_boarding
                        and first
                        and clock_on(cursor, first) > last_boarding
                        and cursor > last_boarding
                    ):
                        last_boarding += timedelta(days=1)
                    steps.append(
                        RouteStep(
                            mode=mode,
                            name=line["name"],
                            origin=line["departure_stop"]["name"],
                            destination=line["arrival_stop"]["name"],
                            departure=cursor,
                            arrival=end,
                            distance_m=int(line.get("distance") or 0),
                            last_boarding=last_boarding,
                        )
                    )
                    cursor = end
                taxi = segment.get("taxi") or {}
                if taxi:
                    seconds = int(taxi.get("drivetime") or 0)
                    if seconds <= 0:
                        raise ValueError("missing_taxi_duration")
                    end = cursor + timedelta(seconds=seconds)
                    steps.append(
                        RouteStep(
                            mode=Mode.taxi,
                            name="短途打车接驳",
                            origin=taxi.get("startname") or origin,
                            destination=taxi.get("endname") or "接续站点",
                            departure=cursor,
                            arrival=end,
                            distance_m=int(taxi.get("distance") or 0),
                        )
                    )
                    cursor = end
                    cost_incomplete = True
                    warnings.append("含打车接驳，地图交通参考金额不保证覆盖实际打车支出")
                if segment.get("airplane"):
                    raise ValueError("unsupported_route_segment")
            if not steps:
                raise ValueError("empty_route")
            duration = int((raw.get("cost") or {}).get("duration") or 0)
            cursor = max(cursor, at + timedelta(seconds=duration))
            routes.append(
                RouteEvidence(
                    origin=origin,
                    destination=destination,
                    departure=at,
                    arrival=cursor,
                    cost_cents=cents((raw.get("cost") or {}).get("transit_fee")),
                    steps=steps,
                    queried_at=datetime.now(TZ),
                    warnings=list(dict.fromkeys(warnings)),
                    cost_incomplete=cost_incomplete,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(str(exc) if isinstance(exc, ValueError) else "incomplete_route_fields")
    return routes, rejected
