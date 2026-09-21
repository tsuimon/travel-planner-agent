"""Policy retrieval exposed through the same bounded tool registry."""

import asyncio
from datetime import date
from pydantic import Field
from src.domain import Model
from src.tools.base import BaseTool


class PolicyArgs(Model):
    question: str = Field(min_length=1, max_length=3000)
    today: date


class PolicyTool(BaseTool):
    name, description, args_model = "rag_query", "检索已收录的交通政策并返回来源和复核日期", PolicyArgs
    ttl = 3600

    def __init__(self, settings, cache, repo, knowledge) -> None:
        super().__init__(settings, cache, repo)
        self.knowledge = knowledge

    async def execute(self, args: PolicyArgs) -> dict:
        answer, sources = await asyncio.to_thread(self.knowledge.answer, args.question, args.today)
        return {"answer": answer, "sources": sources}
