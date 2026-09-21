"""AMap Web Service POI adapter; business hours are deliberately not inferred."""

import httpx
from pydantic import Field
from src.domain import Model
from src.tools.base import BaseTool


class PlaceArgs(Model):
    keywords: str = Field(min_length=1, max_length=80)
    city: str = ""


class Place(Model):
    id: str
    name: str
    address: str
    location: str
    city: str
    citycode: str = ""
    opening_hours: str = ""
    source: str = "https://restapi.amap.com/v3/place/text"


class AmapPlacesTool(BaseTool):
    name, description, args_model = "amap_places", "查询真实场馆/餐饮门店候选，不保证营业时间", PlaceArgs
    ttl = 86400
    cache_version = 3

    async def execute(self, args: PlaceArgs) -> dict:
        key = self.settings.amap_api_key.get_secret_value()
        if not key:
            raise RuntimeError("amap_key_not_configured")
        try:
            async with httpx.AsyncClient(timeout=self.settings.tool_timeout) as client:
                response = await client.get(
                    "https://restapi.amap.com/v3/place/text",
                    params={
                        "key": key,
                        "keywords": args.keywords,
                        "city": args.city,
                        "citylimit": "true" if args.city else "false",
                        "offset": 5,
                        "extensions": "base",
                    },
                )
                response.raise_for_status()
                body = response.json()
            if body.get("status") != "1" or not isinstance(body.get("pois"), list):
                raise ValueError("invalid_amap_response")
            places = []
            for value in body["pois"][:5]:

                def string(name: str) -> str:
                    item = value.get(name, "")
                    return item if isinstance(item, str) else ""

                place = Place(
                    id=string("id"),
                    name=string("name"),
                    address=string("address"),
                    location=string("location"),
                    city=string("cityname"),
                    citycode=string("citycode"),
                    opening_hours=str((value.get("biz_ext") or {}).get("opentime2") or ""),
                )
                if place.id and place.name and place.location:
                    places.append(place.model_dump())
            return {"places": places, "business_hours_verified": False}
        except httpx.HTTPError as exc:
            # HTTP errors can include the query-string key; never expose their text.
            raise RuntimeError("amap_request_failed") from exc
