"""Official AMap geocoding and routing, with the shared timeout/retry/cache policy."""

from datetime import datetime
import httpx
from pydantic import Field
from typing import Literal
from src.domain import Model
from src.tools.base import BaseTool


class GeoArgs(Model):
    address: str = Field(min_length=1, max_length=128)


class RouteArgs(Model):
    origin: str = Field(pattern=r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$")
    destination: str = Field(pattern=r"^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$")
    city1: str
    city2: str
    at: datetime
    mode: Literal["transit", "walk", "drive"] = "transit"
    strategy: Literal["0", "1", "2", "8"] = "1"


class AmapHTTPTool(BaseTool):
    async def get(self, path: str, params: dict) -> dict:
        key = self.settings.amap_api_key.get_secret_value()
        if not key:
            raise RuntimeError("amap_key_missing")
        try:
            async with httpx.AsyncClient(timeout=self.settings.tool_timeout) as client:
                response = await client.get("https://restapi.amap.com" + path, params={**params, "key": key})
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("status") != "1":
                raise ValueError("amap_api_rejected")
            return data
        except httpx.HTTPError:
            raise RuntimeError("amap_http_failed") from None


class AmapGeocodeTool(AmapHTTPTool):
    name, description, args_model = "amap_geocode", "解析真实地址坐标和城市编码", GeoArgs
    ttl = 86400

    async def execute(self, args: GeoArgs) -> dict:
        data = await self.get("/v3/geocode/geo", {"address": args.address})
        values = []
        for row in data.get("geocodes", [])[:3]:
            if row.get("location") and row.get("citycode"):
                values.append({k: row.get(k) for k in ("location", "citycode", "formatted_address", "level")})
        return {"locations": values}


class AmapRouteTool(AmapHTTPTool):
    name, description, args_model = "amap_route", "查询真实公共交通路线或步行接驳候选", RouteArgs
    ttl = 120

    async def execute(self, args: RouteArgs) -> dict:
        if args.at.tzinfo is None:
            raise ValueError("route_datetime_requires_timezone")
        params = {"origin": args.origin, "destination": args.destination, "show_fields": "cost"}
        if args.mode == "transit":
            params.update(
                city1=args.city1,
                city2=args.city2,
                date=args.at.strftime("%Y-%m-%d"),
                time=args.at.strftime("%H-%M"),
                strategy=args.strategy,
                nightflag="1",
                AlternativeRoute="5",
            )
        path = {
            "walk": "/v5/direction/walking",
            "drive": "/v5/direction/driving",
            "transit": "/v5/direction/transit/integrated",
        }[args.mode]
        return await self.get(path, params)


class LineArgs(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9]+$", max_length=64)


class AmapLineTool(AmapHTTPTool):
    """Expand observed lines into station candidates; never infer station arrival clocks."""

    name, description, args_model = "amap_line", "查询已知公交地铁线路的有序站点", LineArgs
    ttl = 86400

    async def execute(self, args: LineArgs) -> dict:
        data = await self.get("/v3/bus/lineid", {"id": args.id, "extensions": "all"})
        return {
            "lines": [
                {k: row.get(k) for k in ("id", "name", "status", "busstops")}
                for row in data.get("buslines", [])[:1]
            ]
        }
