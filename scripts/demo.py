"""Run all supported demo queries and record concrete results."""

import asyncio
import json
from pathlib import Path
from src.agent.graph import TravelService
from src.config import Settings
from src.domain import ChatRequest


async def main() -> None:
    service = TravelService(Settings())
    queries = [
        "明天晚上从北京海淀到天津滨海新区，预算100以内，不要打车",
        "明天22:40从夜间起点到夜间终点，预算100元，能骑共享单车，这次可以打车",
        "明天上午8:00从甲县到乙县，预算100元",
        "高铁能带充电宝吗",
    ]
    results = []
    for query in queries:
        result = await service.chat(ChatRequest(message=query))
        print(query, result.status, len(result.plans))
        results.append(result.model_dump(mode="json"))
    Path("data").mkdir(exist_ok=True)
    Path("data/demo-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
