"""Request-local node implementation; graph instances never share mutable request state."""

import asyncio
import logging
import re
from datetime import datetime

from src.agent.budget import Budget
from src.agent.parser import parse, route_match, night_clock_assumed
from src.agent.time_text import normalize_time_text
from src.agent.time_roles import apply_period_reply, ground_model_times, arrival_match
from src.agent.explicit_limits import explicit_limits
from src.agent.intent import is_itinerary
from src.agent.itinerary_parser import explain_draft, missing_fields, preserve_known_fields, rule_draft
from src.agent.state import AgentState
from src.domain import (
    ChatRequest,
    ChatResponse,
    Constraints,
    ItineraryDraft,
    ItineraryStop,
    Mode,
    Network,
    Preferences,
    Weather,
)
from src.errors import AmbiguousArrivalTime, NeedsClarification, TokenLimit
from src.memory import is_preference_only, merge_preferences, preference_patch, should_remember
from src.risk import annotate, describe
from src.search.algorithm import SearchStats
from src.search.planner import HierarchicalPlanner, rank
from src.search.itinerary import ItineraryPlanner
from src.agent.intelligent import IntelligentPlanner, describe_report


class RequestNodes:
    """Dependencies are passed by the service; each instance handles exactly one request."""

    def __init__(self, service, request: ChatRequest, sid: str, now: datetime) -> None:
        self.service, self.request, self.sid, self.now = service, request, sid, now
        self.budget = Budget(service.settings.planning_timeout, service.settings.token_limit)
        self.latest: AgentState = {
            "query": request.message,
            "iteration_count": 0,
            "token_usage": 0,
            "errors": [],
            "combined_plans": [],
            "trace": [],
            "tool_outputs": {},
        }
        self.constraints: Constraints | None = None
        self.itinerary: ItineraryDraft | None = None
        self.network: Network | None = None
        self.weather = Weather()
        self.candidates = []
        self.place_candidates: list[dict] = []
        self.intelligent: IntelligentPlanner | None = None
        self.pending_time_query: str | None = None
        self.conversation = None
        self.stats = SearchStats(limit=service.settings.max_expansions, deadline=self.budget.deadline)

    def update(self, node: str, **values) -> AgentState:
        self.latest.update(values)
        self.latest["trace"] = self.latest["trace"] + [node]
        self.latest["token_usage"] = self.budget.used
        return dict(self.latest)

    def error(self, code: str) -> None:
        self.latest["errors"] = list(dict.fromkeys([*self.latest["errors"], code]))

    def response(self, status: str, answer: str, **kwargs) -> dict:
        return ChatResponse(
            session_id=self.sid,
            status=status,
            answer=answer,
            constraints=self.constraints,
            metadata={
                "data_mode": self.service.settings.data_mode,
                "iterations": self.latest["iteration_count"],
                "token_reserved": self.budget.used,
                "token_actual": self.budget.actual,
                "errors": self.latest["errors"],
                "expansions": self.stats.expansions,
                "search_truncated": self.stats.truncated,
                "trace": self.latest["trace"],
                "night_branch": self.stats.night_branches > 0,
                "itinerary_draft": self.itinerary.model_dump(mode="json") if self.itinerary else None,
                "place_candidates": self.place_candidates,
                "pending_time_query": self.pending_time_query,
                "conversation": self.conversation.metadata() if self.conversation else None,
                "intelligent_plan": self.intelligent.report.model_dump(mode="json")
                if self.intelligent
                else None,
            },
            **kwargs,
        ).model_dump(mode="json")

    async def parse_requirements(self, state: AgentState) -> AgentState:
        if (
            self.service.settings.conversation_agent
            and self.service.llm.enabled
            and not self.request.constraints
            and not self.request.itinerary
        ):
            from src.agent.conversation import ConversationAgent

            self.conversation = ConversationAgent(self)
            return await self.conversation.run()
        query = normalize_time_text(self.request.message)
        prefs = self.service.repo.preferences()
        patch = preference_patch(query, prefs)
        effective = merge_preferences(prefs, patch)
        history = self.service.repo.history(self.sid, 6)
        latest_payload = next(
            (r["payload"] for r in reversed(history) if r["role"] == "assistant" and r.get("payload")), {}
        )
        previous_draft = latest_payload.get("metadata", {}).get("itinerary_draft")
        pending = latest_payload.get("metadata", {}).get("pending_time_query")
        if pending:
            query = apply_period_reply(pending, query) or query
            patch = preference_patch(query, prefs)
            effective = merge_preferences(prefs, patch)
        new_single_route = bool(route_match(query)) and not is_itinerary(query)
        active_trip = bool(previous_draft or latest_payload.get("constraints"))
        if active_trip and not new_single_route:
            remembered_trip = (previous_draft or {}).get("preferences") or {
                k: v
                for k, v in (latest_payload.get("constraints") or {}).items()
                if k in Preferences.model_fields
            }
            prefs = Preferences.model_validate(remembered_trip)
            patch = preference_patch(query, prefs)
            effective = merge_preferences(prefs, patch)
        explicit_memory = bool(re.search(r"记住|以后|今后|长期|默认|平时", query))
        if patch and should_remember(query) and (not active_trip or explicit_memory):
            self.service.repo.save_preferences(effective)
        previous_is_single = (
            previous_draft and len(previous_draft.get("stops", [])) == 1 and latest_payload.get("constraints")
        )
        if (
            self.request.itinerary
            or is_itinerary(query)
            or (
                previous_draft
                and not previous_is_single
                and not new_single_route
                and not self.request.constraints
            )
        ):
            previous = ItineraryDraft.model_validate(previous_draft) if previous_draft else None
            if previous:
                effective = merge_preferences(previous.preferences, patch)
            self.itinerary = self.request.itinerary
            if self.itinerary is None:
                try:
                    self.itinerary = rule_draft(query, effective, previous)
                except ValueError:
                    self.itinerary = previous or ItineraryDraft(preferences=effective)
                    self.error("invalid_itinerary_update")
                if self.service.llm.enabled:
                    try:
                        interpreted = await self.service.llm.parse_itinerary(
                            {
                                "query": query,
                                "now": self.now.isoformat(),
                                "previous": previous_draft,
                                "preferences": effective.model_dump(mode="json"),
                            },
                            self.budget,
                        )
                        # A model may not silently drop stops already identified by the rule parser/history.
                        if len(interpreted.stops) < len(self.itinerary.stops):
                            raise ValueError("model_dropped_itinerary_stops")
                        interpreted = preserve_known_fields(interpreted, self.itinerary, previous)
                        if (
                            interpreted.arrive_by
                            and any(s.start_at == interpreted.arrive_by for s in interpreted.stops[:-1])
                            and self.itinerary.arrive_by is None
                        ):
                            interpreted.arrive_by = None
                            self.error("event_time_misused_as_final_deadline")
                        interpreted.preferences = merge_preferences(interpreted.preferences, patch)
                        interpreted.preferences.excluded_modes = list(
                            set(interpreted.preferences.excluded_modes) | set(effective.excluded_modes)
                        )
                        interpreted.preferences = merge_preferences(interpreted.preferences, patch)
                        self.itinerary = interpreted
                    except (ValueError, RuntimeError, KeyError, TypeError, TokenLimit) as exc:
                        self.error(type(exc).__name__)
            if self.service.settings.amap_api_key.get_secret_value() and not self.request.itinerary:
                self.intelligent = IntelligentPlanner(self.service.registry, self.budget.deadline)
                return self.update("parse_requirements", terminal=False)
            if missing_fields(self.itinerary):
                suggestions = await self.suggest_places()
                return self.update(
                    "parse_requirements",
                    terminal=True,
                    response=self.response(
                        "clarification",
                        explain_draft(self.itinerary, self.service.settings.data_mode) + suggestions,
                    ),
                )
            if self.service.settings.data_mode == "live" and self.itinerary.depart_after < self.now:
                return self.update(
                    "parse_requirements",
                    terminal=True,
                    response=self.response("clarification", "出发时间已过去，请调整行程日期。"),
                )
            return self.update("parse_requirements", terminal=False)
        if self.request.constraints:
            self.constraints = self.request.constraints
        elif any(x in query for x in ("充电宝", "安检", "退票", "退改签", "停放规则", "单车规则")):
            result = await self.service.registry.call(
                "rag_query", {"question": query, "today": self.now.date()}, self.budget.deadline
            )
            answer = result.data.get("answer", "政策检索暂不可用，请查询官方渠道。")
            sources = result.data.get("sources", [])
            if not result.success:
                self.error("rag_unavailable")
            return self.update(
                "parse_requirements", terminal=True, response=self.response("policy", answer, sources=sources)
            )
        elif patch and is_preference_only(query) and not active_trip:
            message = (
                "已保存偏好，下次规划会自动使用。"
                if should_remember(query)
                else "本次偏好已识别，请补充行程。"
            )
            return self.update("parse_requirements", terminal=True, response=self.response("memory", message))
        else:
            history = self.service.repo.history(self.sid, 6)
            prior = next(
                (
                    r["payload"].get("constraints")
                    for r in reversed(history)
                    if r["role"] == "assistant" and r.get("payload")
                ),
                None,
            )
            previous = Constraints.model_validate(prior) if prior and not new_single_route else None
            # Rule-derived explicit values override model interpretation whenever available.
            rule = None
            rule_error = None
            try:
                rule = parse(query, effective, self.now, previous)
            except AmbiguousArrivalTime as exc:
                self.constraints = previous
                self.pending_time_query = query
                return self.update(
                    "parse_requirements", terminal=True, response=self.response("clarification", str(exc))
                )
            except NeedsClarification as exc:
                rule_error = str(exc)
            if self.service.llm.enabled:
                try:
                    parsed = await self.service.llm.parse(
                        {
                            "query": query,
                            "now": self.now.isoformat(),
                            "preferences": effective.model_dump(mode="json"),
                            "previous": prior if previous else None,
                        },
                        self.budget,
                    )
                    grounded = ground_model_times(parsed.model_dump(), query, self.now, previous)
                    grounded.update(explicit_limits(query)[0])
                    if previous:
                        for field, default in (
                            ("max_walk_m", 5000),
                            ("max_bike_m", 12000),
                            ("max_transfers", None),
                        ):
                            if field not in explicit_limits(query)[0] and grounded.get(field) == default:
                                grounded[field] = getattr(previous, field)
                    if rule:
                        # Preserve extracted facts without replacing richer model constraints with defaults.
                        fields = ["depart_after", "depart_before", "arrival_priority"]
                        if route_match(query) or previous is None:
                            fields += ["origin", "destination"]
                        relative_change = bool(
                            re.search(r"提前|推迟|延后|晚(?:半|[一二两三\d]+).*小时", query)
                        )
                        if relative_change and previous:
                            fields = [
                                f
                                for f in fields
                                if f not in {"depart_after", "depart_before", "arrival_priority"}
                            ]
                        for field in fields:
                            grounded[field] = getattr(rule, field)
                        if (
                            arrival_match(query)
                            or "最晚" in query
                            or (previous and not new_single_route and not relative_change)
                            or grounded.get("arrive_by") is None
                        ):
                            grounded["arrive_by"] = rule.arrive_by
                        if rule.budget_cents is not None or "不限预算" in query:
                            grounded["budget_cents"] = rule.budget_cents
                    self.constraints = Constraints.model_validate(grounded)
                    # Explicit disallowances always survive the LLM boundary.
                    values = self.constraints.model_dump()
                    values["excluded_modes"] = list(
                        set(self.constraints.excluded_modes) | set(effective.excluded_modes)
                    )
                    if "cycling_acceptance" in patch:
                        values["cycling_acceptance"] = effective.cycling_acceptance
                    values.update(patch)
                    self.constraints = Constraints.model_validate(values)
                except (ValueError, RuntimeError, KeyError, TypeError, TokenLimit) as exc:
                    self.error(type(exc).__name__)
            if self.constraints is None:
                self.constraints = rule
            if self.constraints is None:
                return self.update(
                    "parse_requirements",
                    terminal=True,
                    response=self.response("clarification", rule_error or "请补充明确的行程和约束。"),
                )
        if self.constraints.depart_after < self.now and (
            self.service.settings.data_mode == "live"
            or (self.service.settings.amap_api_key.get_secret_value() and not self.request.constraints)
        ):
            if self.constraints.depart_before >= self.now:
                self.constraints.depart_after = self.now
            else:
                return self.update(
                    "parse_requirements",
                    terminal=True,
                    response=self.response("clarification", "出发时间已经过去，请提供未来的出发时间。"),
                )
        if self.service.settings.amap_api_key.get_secret_value() and not self.request.constraints:
            c = self.constraints
            mixed = bool(re.search(r"地铁.*(?:然后|再|接着).*打车|下车.*打车", query))
            if not new_single_route and previous_draft:
                mixed = mixed or previous_draft.get("metro_then_taxi", False)
            self.itinerary = ItineraryDraft(
                origin=c.origin,
                depart_after=c.depart_after,
                depart_before=c.depart_before,
                arrive_by=c.arrive_by,
                budget_cents=c.budget_cents,
                preferences=Preferences(**{k: getattr(c, k) for k in Preferences.model_fields}),
                stops=[ItineraryStop(locations=[c.destination], duration_min=0)],
                metro_then_taxi=mixed,
                arrival_priority=c.arrival_priority,
                max_walk_m=c.max_walk_m,
                max_bike_m=c.max_bike_m,
                max_transfers=c.max_transfers,
            )
            if night_clock_assumed(query):
                self.itinerary.assumptions.append(
                    f"根据赶末班语境，将出发时间按晚上{c.depart_after:%H:%M}理解；可直接更正"
                )
            if not re.search(r"今天|明天|后天|\d{4}|\d+月|\d+[日号]", query) and new_single_route:
                self.itinerary.assumptions.append(f"未指定日期，按{c.depart_after:%Y-%m-%d}测算")
            self.intelligent = IntelligentPlanner(self.service.registry, self.budget.deadline)
        return self.update(
            "parse_requirements", constraints=self.constraints.model_dump(mode="json"), terminal=False
        )

    async def suggest_places(self) -> str:
        """Offer official POI names without choosing a branch or assuming opening hours."""
        if not self.itinerary or not self.service.settings.amap_api_key.get_secret_value():
            return ""
        generic = {"海底捞", "餐厅", "饭店", "酒店"}
        # Bound searches to the first two unresolved places and keep location selection explicit.
        targets = [
            (i, s) for i, s in enumerate(self.itinerary.stops) if any(p in generic for p in s.locations)
        ][:2]
        for index, stop in targets:
            nearby = self.itinerary.stops[index - 1].locations if index else [self.itinerary.origin or ""]
            query = " ".join([*nearby, *stop.locations])[:80]
            result = await self.service.registry.call(
                "amap_places", {"keywords": query}, self.budget.deadline
            )
            if result.success:
                self.place_candidates.extend(
                    {"stop_index": index, **p} for p in result.data.get("places", [])
                )
            else:
                self.error("amap_places_unavailable")
        if not self.place_candidates:
            return ""
        places = "\n".join(f"- {p['name']}：{p['city']}{p['address']}" for p in self.place_candidates)
        return "\n\n高德检索到的地点候选（请确认门店；尚未核实营业时间与绕行成本）：\n\n" + places

    async def plan_search(self, state: AgentState) -> AgentState:
        self.budget.remaining()
        iteration = state["iteration_count"] + 1
        objective = ["cost", "time", "transfers"][(iteration - 1) % 3]
        return self.update("plan_search", iteration_count=iteration, objective=objective)

    async def execute_tools(self, state: AgentState) -> AgentState:
        self.budget.remaining()
        if self.intelligent and self.itinerary:
            await self.intelligent.plan(self.itinerary)
            return self.update("execute_tools", stop=True)
        if self.itinerary:
            planner = ItineraryPlanner(self.service.registry, self.service.settings)
            self.candidates = await planner.plan(self.itinerary, self.stats)
            for error in planner.errors:
                self.error(error)
            return self.update("execute_tools", stop=True)
        if self.network is not None:
            return self.update("execute_tools")
        registry = self.service.registry
        c = self.constraints
        if c is None:
            raise RuntimeError("constraints_required")
        locations = await asyncio.gather(
            *[
                registry.call(
                    "geocode", {"address": a, "at": c.depart_after.isoformat()}, self.budget.deadline
                )
                for a in (c.origin, c.destination)
            ]
        )
        if not all(r.success for r in locations):
            self.error("geocode_unavailable_or_outside_coverage")
            return self.update("execute_tools", stop=True)
        c = Constraints.model_validate(
            {**c.model_dump(), "origin": locations[0].data["id"], "destination": locations[1].data["id"]}
        )
        self.constraints = c
        calls = [
            (
                "transit_query",
                {
                    "origin": c.origin,
                    "destination": c.destination,
                    "depart_after": c.depart_after.isoformat(),
                    "arrive_by": c.arrive_by.isoformat(),
                },
            ),
            (
                "weather_query",
                {"location": locations[1].data["city"], "date": c.depart_after.date().isoformat()},
            ),
        ]
        if self.service.llm.enabled:
            try:
                optional = await self.service.llm.select_tools(
                    {"query": self.request.message},
                    registry.schemas(["weather_query", "web_search"]),
                    self.budget,
                )
                # Weather arguments are derived by code to keep date/location consistent.
                calls.extend((x.name, x.arguments) for x in optional if x.name == "web_search")
            except (ValueError, RuntimeError, KeyError, TypeError, TokenLimit) as exc:
                self.error(type(exc).__name__)
        results = await asyncio.gather(
            *[registry.call(name, args, self.budget.deadline) for name, args in calls]
        )
        output = {name: r.model_dump(mode="json") for (name, _), r in zip(calls, results)}
        for (name, _), result in zip(calls, results):
            if not result.success:
                self.error(name + "_unavailable")
        if results[0].success:
            self.network = Network.model_validate(results[0].data)
        if results[1].success:
            self.weather = Weather.model_validate(results[1].data)
        return self.update(
            "execute_tools",
            tool_outputs=output,
            constraints=c.model_dump(mode="json"),
            stop=self.network is None,
        )

    async def combine_results(self, state: AgentState) -> AgentState:
        if self.network and self.constraints and not state.get("stop"):
            # First three passes cover common modes; next three add county/ferry alternatives.
            network = self.network
            if state["iteration_count"] <= 3:
                network = Network(
                    nodes=network.nodes,
                    edges=[e for e in network.edges if e.mode not in {Mode.county_bus, Mode.ferry}],
                    coverage=network.coverage,
                )
            planner = HierarchicalPlanner(self.service.settings.transfer_buffer_min)
            paths = await asyncio.to_thread(
                planner.plan, network, self.constraints, state["objective"], self.stats
            )
            self.candidates = list({p.id: p for p in self.candidates + paths}.values())
        return self.update(
            "combine_results", combined_plans=[p.model_dump(mode="json") for p in self.candidates[:15]]
        )

    async def reflect(self, state: AgentState) -> AgentState:
        stop = state.get("stop", False)
        if state["iteration_count"] >= self.service.settings.max_iterations:
            if not self.candidates:
                self.error("max_iterations")
            stop = True
        if self.stats.expansions >= self.stats.limit:
            self.error("expansion_limit")
            stop = True
        if self.budget.used >= self.budget.token_limit:
            self.error("token_limit")
            stop = True
        if len(self.candidates) >= 2 and state["iteration_count"] >= 3:
            stop = True
        return self.update("reflect", stop=stop)

    async def generate_output(self, state: AgentState) -> AgentState:
        if state.get("terminal"):
            return self.update("generate_output")
        if self.intelligent:
            report = self.intelligent.report
            return self.update(
                "generate_output",
                response=self.response(
                    "clarification" if report.questions and not report.options else "degraded",
                    describe_report(report),
                ),
            )
        if self.itinerary:
            if self.candidates:
                plans = rank(self.candidates, self.itinerary.preferences)
                return self.update(
                    "generate_output",
                    response=self.response(
                        "degraded" if self.latest["errors"] else "ok", describe(plans), plans=plans
                    ),
                )
            message = "当前数据中未找到能按顺序完成全部活动、符合总预算与到达期限的方案。"
            if self.latest["errors"]:
                message += "部分地点或交通数据不可用，尚不能判断完整行程是否可行。"
            if self.service.settings.data_mode == "demo":
                message += "当前仅有预设演示地点，真实行程需要接入交通数据。"
            return self.update(
                "generate_output",
                response=self.response("degraded" if self.latest["errors"] else "no_results", message),
            )
        if self.constraints and self.candidates:
            cached = state.get("tool_outputs", {}).get("transit_query", {}).get("cached", False)
            plans = [annotate(p, self.weather, cached) for p in rank(self.candidates, self.constraints)]
            answer = describe(plans)
            if self.latest["errors"]:
                answer += "\n\n部分查询或计算受限，以上仅包含已校验的候选结果。"
            response = self.response("degraded" if self.latest["errors"] else "ok", answer, plans=plans)
        else:
            message = (
                "当前数据覆盖和搜索范围内未找到满足全部约束的方案。可以调整出发时间、预算或交通方式后重试。"
            )
            if "geocode_unavailable_or_outside_coverage" in self.latest["errors"]:
                message = "地址未能解析或不在当前数据覆盖内。演示可使用北京海淀、天津滨海新区、夜间起点/夜间终点、甲县/乙县。"
            response = self.response("degraded" if self.latest["errors"] else "no_results", message)
        logging.getLogger("travel.agent").info(
            "planning_finished status=%s iterations=%d", response["status"], self.latest["iteration_count"]
        )
        return self.update("generate_output", response=response)

    async def fallback(self, reason: str) -> dict:
        if self.conversation:
            return self.conversation.fallback(reason)
        self.error(reason)
        result = await self.generate_output(self.latest)
        return result["response"]
