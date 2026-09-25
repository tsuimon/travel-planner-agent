"""Optional JSON/function-calling client with strict validation and token reservations."""

import asyncio
import json
from typing import Literal

import httpx
from pydantic import Field, ValidationError
from src.agent.budget import Budget
from src.config import Settings
from src.domain import ItineraryDraft, Model
from src.data.cache import TTLCache


class ParsedIntent(Model):
    origin: str
    destination: str
    depart_after: str | None
    depart_before: str | None
    arrive_by: str | None
    budget_cents: int | None = Field(None, ge=0)
    cycling_acceptance: int = Field(0, ge=0, le=2)
    transfer_tolerance: int = Field(2, ge=0, le=3)
    budget_preference: Literal["balanced", "economy", "fast"] = "balanced"
    comfort_priority: Literal["low", "medium", "high"] = "medium"
    excluded_modes: list[str] = Field(default_factory=list)
    max_walk_m: int = Field(5000, ge=0, le=20000)
    max_bike_m: int = Field(12000, ge=0, le=50000)
    max_transfers: int | None = Field(None, ge=0, le=20)


class ToolCall(Model):
    name: Literal["weather_query", "web_search"]
    arguments: dict


PARSE_PROMPT = """你只解析出行需求，返回JSON，不编造地点、时刻、预算。只提取用户表达和给定偏好。
时间使用带+08:00的ISO格式，晚上18:00-23:59；预算单位为分；默认最晚到达为出发窗口后12小时。
不要打车对应excluded_modes=[\"taxi\"]；不骑车cycling_acceptance=0。不能确定的必填信息应返回{}。
必须区分时间角色：X点到/到达/抵达是arrive_by，X点出发才是depart_after；只给到达期限时不可把它复制成出发时刻。
只指定到达期限、未指定出发时刻时，depart_after和depart_before可为null，由程序倒推。
最多N次换乘是max_transfers硬上限，少换乘只是transfer_tolerance软偏好；步行与骑行距离上限使用米。
仅从用户当前输入提取本次需求，历史用于消解指代，不允许放宽用户硬约束。"""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = TTLCache(64)

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.llm_base_url
            and self.settings.llm_model
            and self.settings.llm_api_key.get_secret_value()
        )

    async def completion(self, body: dict, budget: Budget, max_tokens: int = 700) -> dict:
        payload = {
            **self.settings.llm_extra_body,
            "model": self.settings.llm_model,
            **body,
            "max_tokens": max_tokens,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cached = self.cache.get(serialized)
        if cached is not None:
            return cached
        for attempt in range(3):
            reservation = budget.reserve(serialized, max_tokens)
            try:
                async with httpx.AsyncClient(
                    timeout=min(self.settings.tool_timeout, budget.remaining())
                ) as client:
                    response = await asyncio.wait_for(
                        client.post(
                            self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                            headers={
                                "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()
                            },
                            json=payload,
                        ),
                        timeout=min(self.settings.tool_timeout, budget.remaining()),
                    )
                    response.raise_for_status()
                    value = response.json()
                    usage = value.get("usage", {}).get("total_tokens")
                    budget.reconcile(reservation, usage if isinstance(usage, int) and usage >= 0 else None)
                    if not isinstance(value.get("choices"), list) or not value["choices"]:
                        raise ValueError("missing_choices")
                    self.cache.set(serialized, value, 60)
                    return value
            except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    raise RuntimeError("model_unavailable") from exc
                await asyncio.sleep(min(self.settings.tool_backoff * 2**attempt, budget.remaining()))
        raise RuntimeError("model_unavailable")

    async def parse(self, context: dict, budget: Budget) -> ParsedIntent:
        def compact(value):
            if isinstance(value, dict):
                return {k: compact(v) for k, v in value.items() if k not in {"title", "default"}}
            if isinstance(value, list):
                return [compact(v) for v in value]
            return value

        messages = [
            {
                "role": "system",
                "content": PARSE_PROMPT
                + "\n字段:"
                + json.dumps(
                    compact(ParsedIntent.model_json_schema()), ensure_ascii=False, separators=(",", ":")
                ),
            },
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        for attempt in range(2):
            result = await self.completion(
                {"messages": messages, "response_format": {"type": "json_object"}}, budget
            )
            try:
                return ParsedIntent.model_validate_json(result["choices"][0]["message"]["content"])
            except (ValueError, KeyError, TypeError) as exc:
                if attempt:
                    raise ValueError("invalid_model_constraints")
                details = (
                    [{"field": e["loc"], "error": e["msg"]} for e in exc.errors()][:6]
                    if isinstance(exc, ValidationError)
                    else []
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "格式验证失败，请保留用户约束并修复JSON："
                        + json.dumps(details, ensure_ascii=False),
                    }
                )
        raise ValueError("invalid_model_constraints")

    async def select_tools(self, context: dict, schemas: list[dict], budget: Budget) -> list[ToolCall]:
        body = {
            "messages": [
                {"role": "system", "content": "可选查询天气或小众路线线索；只能使用给定工具，不生成班次。"},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "tools": schemas,
            "tool_choice": "auto",
        }
        response = await self.completion(body, budget, max_tokens=300)
        raw = response["choices"][0]["message"].get("tool_calls", [])
        if not isinstance(raw, list) or len(raw) > 2:
            raise ValueError("invalid_tool_calls")
        return [
            ToolCall(name=r["function"]["name"], arguments=json.loads(r["function"]["arguments"]))
            for r in raw
        ]

    async def parse_itinerary(self, context: dict, budget: Budget) -> ItineraryDraft:
        # Compact contract avoids consuming the entire token allowance on nested JSON Schema.
        system = """Return JSON for an ordered trip draft. Extract ONLY user-supplied facts and update previous draft.
Never invent event times, business hours, fares, geographic facts or dates. Unspecified values are null.
Preserve every stop and activity in order, and preserve previous fields unless user changes them.
Fields: origin:string|null, depart_after:ISO datetime|null, depart_before:ISO|null, arrive_by:ISO|null,
stops:[{label:string,locations:[string],start_at:ISO|null,duration_min:int|null,requires_start_time:bool,cost_cents:int|null}],
preferences:{cycling_acceptance:0..2,transfer_tolerance:0..3,budget_preference:balanced|economy|fast,
comfort_priority:low|medium|high,excluded_modes:[walk|taxi|shared_bike|metro|bus|high_speed_rail|normal_rail|flight|coach|county_bus|ferry]},
budget_cents:int|null,budget_scope:transport|total,allow_overnight:bool|null,assumptions:[string],
max_walk_m:int (default 5000),max_bike_m:int (default 12000),max_transfers:int|null.
Distance limits are cumulative meters across the whole trip; explicit maximum transfers is a hard limit.
Keep Chinese number constraints too: 只能走五百米 means max_walk_m=500. 少换乘 is only a preference.
Times must include +08:00. Unknown year/month/day stays null and ask in assumptions. Money is CNY cents.
Concert/meeting start times are fixed anchors and require requires_start_time=true; meals need durations.
Final destination has duration_min=0. Multiple locations for ONE stop are acceptable alternatives, not sequential visits.
Use stable activity labels: 演唱会 for concerts, 用餐 for meals, 到达 for final arrival.
Assumptions must be short user-facing questions, never reasoning traces or explanations of JSON fields.
If restaurants are not specified, retain restaurant keywords; do not invent a branch.
If user says now located at X, that states origin, NOT departure time. No permission to overnight => null.
Never omit excluded modes, constraints or previous stops to produce a simpler answer."""
        system += "\narrive_by is ONLY the final destination deadline, NEVER the concert start time. If no final deadline is supplied return null. Convert explicit 散场 time minus concert start into duration_min. Do not add any fields beyond this contract."
        system += "\nMeals after an event have start_at=null: travel to the restaurant takes time. Never derive fixed meal start from concert finish. Use requires_start_time=true for ANY explicit user-fixed start time, and false for flexible activities."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        for attempt in range(2):
            result = await self.completion(
                {"messages": messages, "response_format": {"type": "json_object"}}, budget, max_tokens=1100
            )
            try:
                return ItineraryDraft.model_validate_json(result["choices"][0]["message"]["content"])
            except (ValueError, TypeError, KeyError):
                if attempt:
                    raise ValueError("invalid_itinerary_draft")
                messages.append(
                    {
                        "role": "user",
                        "content": "Invalid JSON/schema. Repair without inventing missing facts.",
                    }
                )
        raise ValueError("invalid_itinerary_draft")
