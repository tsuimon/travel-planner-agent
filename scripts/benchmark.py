"""Measure an offline demo workload; does not benchmark external APIs or LLMs."""

import asyncio
import json
import platform
import statistics
from pathlib import Path
from time import perf_counter

from src.agent.graph import TravelService
from src.config import Settings
from src.domain import ChatRequest


async def main() -> None:
    service = TravelService(
        Settings(
            _env_file=None,
            database_url="sqlite:///data/benchmark.db",
            chroma_path="data/benchmark-chroma",
            enable_ui=False,
        )
    )
    timings = []
    for _ in range(25):
        start = perf_counter()
        result = await service.chat(
            ChatRequest(message="明天晚上从北京海淀到天津滨海新区，预算50元，不要打车")
        )
        assert result.plans
        timings.append((perf_counter() - start) * 1000)
    report = {
        "workload": "25 sequential synthetic Beijing-Tianjin requests; no network or LLM",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "count": len(timings),
        "mean_ms": statistics.mean(timings),
        "p50_ms": statistics.median(timings),
        "p95_ms": sorted(timings)[23],
        "max_ms": max(timings),
        "all_ms": timings,
    }
    Path("data/benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "all_ms"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
