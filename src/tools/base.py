"""A single transport policy for all provider calls."""

import asyncio
import hashlib
import json
from time import monotonic
from typing import Any

from pydantic import BaseModel, Field
from src.config import Settings
from src.data.cache import TTLCache
from src.data.repository import Repository
from src.errors import DeadlineExceeded


class ToolResult(BaseModel):
    success: bool
    data: dict = Field(default_factory=dict)
    error: str | None = None
    cached: bool = False
    attempts: int = 0


class BaseTool:
    name: str = "base"
    description: str = ""
    args_model: type[BaseModel]
    ttl: int = 300
    cacheable: bool = True
    cache_version: int = 1

    def __init__(self, settings: Settings, cache: TTLCache, repo: Repository | None = None) -> None:
        self.settings, self.cache, self.repo = settings, cache, repo

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }

    async def execute(self, args: BaseModel) -> dict:
        raise NotImplementedError

    async def call(self, params: dict, deadline: float, refresh: bool = False) -> ToolResult:
        try:
            args = self.args_model.model_validate(params)
        except ValueError:
            return ToolResult(success=False, error="invalid_tool_arguments")
        identity = json.dumps(
            [
                self.name,
                self.cache_version,
                self.settings.data_mode,
                self.settings.provider_url,
                args.model_dump(mode="json"),
            ],
            sort_keys=True,
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        cached = self.cache.get(key) if self.cacheable else None
        if cached is None and self.repo and self.cacheable:
            cached = self.repo.cache_get(key)
        if cached is not None and not refresh:
            return ToolResult(success=True, data=cached, cached=True)
        error = "unavailable"
        for attempt in range(3):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise DeadlineExceeded()
            try:
                data = await asyncio.wait_for(self.execute(args), min(remaining, self.settings.tool_timeout))
                if self.cacheable:
                    self.cache.set(key, data, self.ttl)
                    if self.repo:
                        self.repo.cache_set(key, data, self.ttl)
                return ToolResult(success=True, data=data, attempts=attempt + 1)
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
                error = type(exc).__name__
            if attempt < 2:
                pause = self.settings.tool_backoff * 2**attempt
                if monotonic() + pause >= deadline:
                    break
                await asyncio.sleep(pause)
        # Retry time may outlive the cached entry. Recheck freshness before fallback.
        fallback = self.cache.get(key) if self.cacheable else None
        if fallback is None and self.repo and self.cacheable:
            fallback = self.repo.cache_get(key)
        if fallback is not None:
            return ToolResult(success=True, data=fallback, cached=True, error=error, attempts=3)
        return ToolResult(success=False, error=error, attempts=3)


class Registry:
    def __init__(self, tools: list[BaseTool]) -> None:
        self.tools = {t.name: t for t in tools}
        if len(self.tools) != len(tools):
            raise ValueError("重复工具名")

    def schemas(self, names: list[str] | None = None) -> list[dict]:
        return [t.schema() for name, t in self.tools.items() if names is None or name in names]

    async def call(self, name: str, args: dict[str, Any], deadline: float) -> ToolResult:
        if name not in self.tools:
            return ToolResult(success=False, error="unknown_tool")
        return await self.tools[name].call(args, deadline)
