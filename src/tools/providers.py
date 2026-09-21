"""Demo adapters and a documented authorized-provider gateway contract."""

from datetime import datetime
import httpx
from pydantic import Field

from src.domain import Model, Network, Node, Preferences, Weather
from src.tools.base import BaseTool
from src.search.sample import sample_network


class TransitArgs(Model):
    origin: str
    destination: str
    depart_after: datetime
    arrive_by: datetime


class WeatherArgs(Model):
    location: str
    date: str


class GeocodeArgs(Model):
    address: str
    at: datetime


class WebArgs(Model):
    query: str = Field(min_length=1, max_length=512)


class WebItem(Model):
    title: str
    url: str
    snippet: str


class WebResults(Model):
    results: list[WebItem] = Field(default_factory=list, max_length=5)


class GatewayTool(BaseTool):
    async def gateway(self, args: Model, output: type[Model]) -> dict:
        if not self.settings.provider_url:
            raise RuntimeError("provider_not_configured")
        headers = {"Authorization": "Bearer " + self.settings.provider_api_key.get_secret_value()}
        try:
            async with httpx.AsyncClient(timeout=self.settings.tool_timeout) as client:
                response = await client.post(
                    self.settings.provider_url.rstrip("/") + "/" + self.name,
                    json=args.model_dump(mode="json"),
                    headers=headers,
                )
                response.raise_for_status()
                return output.model_validate(response.json()).model_dump(mode="json")
        except httpx.HTTPError as exc:
            raise RuntimeError("provider_http_error") from exc


class TransitTool(GatewayTool):
    name, description, args_model = "transit_query", "查询指定时窗内的交通网络和班次，包含来源", TransitArgs
    ttl = 120

    async def execute(self, args: TransitArgs) -> dict:
        if self.settings.data_mode == "demo":
            return sample_network(args.depart_after).model_dump(mode="json")
        value = await self.gateway(args, Network)
        if any(e["demo"] for e in value["edges"]):
            raise ValueError("live供应商不能返回演示数据")
        return value


class WeatherTool(GatewayTool):
    name, description, args_model = "weather_query", "查询日期对应天气；无数据返回未知", WeatherArgs
    ttl = 1800

    async def execute(self, args: WeatherArgs) -> dict:
        if self.settings.data_mode == "demo":
            return Weather(
                condition="演示小雨", rain_probability=0.65, source="synthetic://weather"
            ).model_dump()
        return await self.gateway(args, Weather)


class GeocodeTool(GatewayTool):
    name, description, args_model = "geocode", "把地址解析为交通网络节点；不猜测未知地址", GeocodeArgs
    ttl = 86400

    async def execute(self, args: GeocodeArgs) -> dict:
        if self.settings.data_mode == "demo":
            network = sample_network(args.at)
            aliases = {"北京": "haidian", "天津": "binhai", "滨海新区": "binhai", "海淀": "haidian"}
            value = aliases.get(args.address, args.address)
            matches = [n for n in network.nodes if value in (n.id, n.name)]
            if len(matches) != 1:
                raise ValueError("演示地址不在覆盖内")
            return matches[0].model_dump()
        return await self.gateway(args, Node)


class WebSearchTool(GatewayTool):
    name, description, args_model = "web_search", "搜索待核实交通线索；结果不可直接变成交通边", WebArgs

    async def execute(self, args: WebArgs) -> dict:
        if self.settings.data_mode == "demo":
            return WebResults().model_dump()
        return await self.gateway(args, WebResults)


class PreferenceArgs(Model):
    value: Preferences | None = None


class PreferenceTool(BaseTool):
    name, description, args_model = "user_preferences", "读取或明确更新本地用户长期偏好", PreferenceArgs
    cacheable = False

    async def execute(self, args: PreferenceArgs) -> dict:
        if self.repo is None:
            raise RuntimeError("repository_required")
        if args.value is not None:
            self.repo.save_preferences(args.value)
        return self.repo.preferences().model_dump(mode="json")
