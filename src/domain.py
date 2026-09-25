"""Validated shared schemas. Money is integer CNY cents; times are timezone-aware."""

from datetime import datetime
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

TZ = ZoneInfo("Asia/Shanghai")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mode(str, Enum):
    high_speed_rail = "high_speed_rail"
    normal_rail = "normal_rail"
    flight = "flight"
    coach = "coach"
    county_bus = "county_bus"
    ferry = "ferry"
    metro = "metro"
    bus = "bus"
    taxi = "taxi"
    shared_bike = "shared_bike"
    walk = "walk"


LOCAL = {Mode.metro, Mode.bus, Mode.taxi, Mode.shared_bike, Mode.walk}
PUBLIC = {Mode.metro, Mode.bus}


class Preferences(Model):
    cycling_acceptance: int = Field(0, ge=0, le=2)
    transfer_tolerance: int = Field(2, ge=0, le=3)
    budget_preference: Literal["balanced", "economy", "fast"] = "balanced"
    comfort_priority: Literal["low", "medium", "high"] = "medium"
    excluded_modes: list[Mode] = Field(default_factory=list)


class Constraints(Preferences):
    origin: str = Field(min_length=1, max_length=128)
    destination: str = Field(min_length=1, max_length=128)
    depart_after: datetime
    depart_before: datetime
    arrive_by: datetime
    arrival_priority: bool = False
    budget_cents: int | None = Field(None, ge=0)
    max_walk_m: int = Field(5000, ge=0, le=20000)
    max_bike_m: int = Field(12000, ge=0, le=50000)
    max_transfers: int | None = Field(None, ge=0, le=20)

    @model_validator(mode="after")
    def valid_times(self) -> "Constraints":
        for value in (self.depart_after, self.depart_before, self.arrive_by):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("时间必须带时区")
        if not self.depart_after <= self.depart_before < self.arrive_by:
            raise ValueError("出发时间窗或最晚到达时间冲突")
        if (self.arrive_by - self.depart_after).total_seconds() > 172800:
            raise ValueError("原型只支持48小时内的行程")
        return self


class Node(Model):
    id: str
    name: str
    city: str
    hub: bool = False
    lat: float = Field(0, ge=-90, le=90)
    lng: float = Field(0, ge=-180, le=180)


class Edge(Model):
    id: str
    origin: str
    destination: str
    mode: Mode
    service_id: str
    cost_cents: int = Field(ge=0)
    duration_min: int = Field(gt=0)
    distance_m: int = Field(0, ge=0)
    departures: list[datetime] = Field(default_factory=list)
    # Empty departures means flexible/frequency service within explicit operating intervals.
    operating_start: datetime | None = None
    operating_end: datetime | None = None
    headway_min: int = Field(0, ge=0)
    source: str
    observed_at: datetime
    demo: bool = True
    official: bool = False
    seats: int | None = Field(None, ge=0)
    fare_estimated: bool = False

    @model_validator(mode="after")
    def valid_service(self) -> "Edge":
        for dt in [self.observed_at, *self.departures, self.operating_start, self.operating_end]:
            if dt is not None and (dt.tzinfo is None or dt.utcoffset() is None):
                raise ValueError("班次时间必须带时区")
        if not self.departures and (self.operating_start is None or self.operating_end is None):
            raise ValueError("必须提供实际班次或完整运营区间")
        if self.operating_start and self.operating_end and self.operating_end < self.operating_start:
            raise ValueError("跨午夜需使用次日绝对时间")
        return self


class Network(Model):
    nodes: list[Node]
    edges: list[Edge]
    coverage: str

    @model_validator(mode="after")
    def valid_references(self) -> "Network":
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes) or len({e.id for e in self.edges}) != len(self.edges):
            raise ValueError("节点或边ID重复")
        if any(e.origin not in ids or e.destination not in ids for e in self.edges):
            raise ValueError("交通边引用未知节点")
        return self


class Leg(Model):
    edge_id: str
    origin: str
    destination: str
    origin_name: str
    destination_name: str
    mode: Mode
    service_id: str
    departure: datetime
    arrival: datetime
    cost_cents: int
    distance_m: int
    source: str
    observed_at: datetime
    demo: bool
    official: bool
    fare_estimated: bool = False
    seats: int | None = None
    last_service: bool = False


class Activity(Model):
    label: str
    location: str
    start: datetime
    end: datetime
    cost_cents: int = Field(0, ge=0)


class ItineraryStop(Model):
    label: str = "到达"
    locations: list[str] = Field(default_factory=list, max_length=5)
    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_min: int | None = Field(None, ge=0, le=10080)
    requires_start_time: bool = False
    cost_cents: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_end(self) -> "ItineraryStop":
        if self.end_at:
            if not self.start_at:
                raise ValueError("活动结束时间需要对应的开始时间")
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
                raise ValueError("活动时间必须包含时区")
            minutes = (self.end_at - self.start_at).total_seconds() / 60
            if minutes < 0 or minutes > 10080 or minutes != int(minutes):
                raise ValueError("活动开始和结束时间冲突")
            if self.duration_min is not None and self.duration_min != int(minutes):
                raise ValueError("活动时长与起止时间冲突")
            self.duration_min = int(minutes)
        return self


class ItineraryDraft(Model):
    """Incomplete drafts survive clarification; only complete drafts enter search."""

    origin: str | None = None
    depart_after: datetime | None = None
    depart_before: datetime | None = None
    arrive_by: datetime | None = None
    stops: list[ItineraryStop] = Field(default_factory=list, max_length=8)
    preferences: Preferences = Field(default_factory=Preferences)
    budget_cents: int | None = Field(None, ge=0)
    budget_scope: Literal["transport", "total"] = "transport"
    allow_overnight: bool | None = None
    metro_then_taxi: bool = False
    arrival_priority: bool = False
    max_walk_m: int = Field(5000, ge=0, le=20000)
    max_bike_m: int = Field(12000, ge=0, le=50000)
    max_transfers: int | None = Field(None, ge=0, le=20)
    assumptions: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_times(self) -> "ItineraryDraft":
        times = [self.depart_after, self.depart_before, self.arrive_by, *[s.start_at for s in self.stops]]
        for value in times:
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("行程时间必须包含时区")
        if self.depart_after and self.depart_before and self.depart_after > self.depart_before:
            raise ValueError("出发时间窗冲突")
        if self.depart_after and self.arrive_by:
            seconds = (self.arrive_by - self.depart_after).total_seconds()
            if seconds <= 0 or seconds > 604800:
                raise ValueError("多站行程需在7天以内，且最终到达晚于出发")
        return self


class Plan(Model):
    id: str
    label: str = "综合"
    legs: list[Leg]
    total_cost_cents: int
    total_minutes: float
    transfers: int
    score: float = 0
    risks: list[str] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)


class Weather(Model):
    condition: str = "未知"
    rain_probability: float | None = Field(None, ge=0, le=1)
    source: str = "未接入"
    demo: bool = True


class ChatRequest(Model):
    message: str = Field(min_length=1, max_length=3000)
    session_id: str | None = None
    constraints: Constraints | None = None
    itinerary: ItineraryDraft | None = None

    @model_validator(mode="after")
    def one_request_format(self) -> "ChatRequest":
        if self.constraints and self.itinerary:
            raise ValueError("constraints和itinerary只能提供一个")
        return self


class ChatResponse(Model):
    session_id: str
    status: Literal["ok", "no_results", "clarification", "degraded", "policy", "memory"]
    answer: str
    plans: list[Plan] = Field(default_factory=list)
    constraints: Constraints | None = None
    metadata: dict = Field(default_factory=dict)
    sources: list[dict] = Field(default_factory=list)
