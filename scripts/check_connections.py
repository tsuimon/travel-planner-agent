"""Check configuration without printing secrets; --network runs small provider probes."""

import argparse
import asyncio
from time import monotonic
from src.agent.budget import Budget
from src.agent.llm import LLMClient
from src.config import Settings
from src.data.cache import TTLCache
from src.tools.amap import AmapPlacesTool


async def check(network: bool) -> None:
    settings = Settings()
    llm = LLMClient(settings)
    print("LLM:", "configured" if llm.enabled else "missing base URL / model / key")
    print("AMap:", "configured" if settings.amap_api_key.get_secret_value() else "missing key")
    print(
        "Ticket gateway:",
        "configured" if settings.provider_url else "not configured; ticket inventory unavailable",
    )
    print(
        "Intelligent map routing:",
        "enabled" if settings.amap_api_key.get_secret_value() else "not configured",
    )
    if not network:
        print("No external requests sent. Add --network to test the configured keys.")
        return
    if llm.enabled:
        try:
            result = await llm.completion(
                {
                    "messages": [{"role": "user", "content": 'Return JSON {"ok":true}'}],
                    "response_format": {"type": "json_object"},
                },
                Budget(),
                max_tokens=64,
            )
            import json

            valid = json.loads(result["choices"][0]["message"]["content"]) == {"ok": True}
            print("LLM JSON probe:", "passed" if valid else "unexpected response")
        except Exception as exc:
            print("LLM JSON probe failed:", type(exc).__name__, "(check key, balance, model and base URL)")
    if settings.amap_api_key.get_secret_value():
        tool = AmapPlacesTool(settings, TTLCache())
        result = await tool.call({"keywords": "天津奥林匹克中心体育场", "city": "天津"}, monotonic() + 30)
        print(
            "AMap POI probe:",
            "passed" if result.success else "failed",
            "results:",
            len(result.data.get("places", [])),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network", action="store_true", help="Send a small test request to each configured provider"
    )
    asyncio.run(check(parser.parse_args().network))
