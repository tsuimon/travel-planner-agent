"""Explicit six-node ReAct-style workflow and bounded request execution."""

import asyncio
from datetime import datetime
from time import monotonic

from langgraph.graph import END, START, StateGraph
from src.agent.llm import LLMClient
from src.agent.nodes import RequestNodes
from src.agent.state import AgentState
from src.config import Settings
from src.data.cache import TTLCache
from src.data.repository import Repository
from src.domain import ChatRequest, ChatResponse, TZ
from src.errors import DeadlineExceeded
from src.rag.knowledge import KnowledgeBase
from src.tools.base import Registry
from src.tools.providers import GeocodeTool, PreferenceTool, TransitTool, WeatherTool, WebSearchTool
from src.tools.rag_tool import PolicyTool
from src.tools.amap import AmapPlacesTool
from src.tools.amap_routes import AmapGeocodeTool, AmapRouteTool, AmapLineTool


def build_graph(nodes: RequestNodes):
    graph = StateGraph(AgentState)
    for name in (
        "parse_requirements",
        "plan_search",
        "execute_tools",
        "combine_results",
        "reflect",
        "generate_output",
    ):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "parse_requirements")
    graph.add_conditional_edges(
        "parse_requirements",
        lambda s: "output" if s.get("terminal") else "plan",
        {"output": "generate_output", "plan": "plan_search"},
    )
    graph.add_edge("plan_search", "execute_tools")
    graph.add_edge("execute_tools", "combine_results")
    graph.add_edge("combine_results", "reflect")
    graph.add_conditional_edges(
        "reflect",
        lambda s: "output" if s.get("stop") else "retry",
        {"output": "generate_output", "retry": "plan_search"},
    )
    graph.add_edge("generate_output", END)
    return graph.compile()


class TravelService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo = Repository(settings.database_url)
        self.cache = TTLCache()
        self.knowledge = KnowledgeBase(settings.chroma_path)
        self.registry = Registry(
            [
                cls(settings, self.cache, self.repo)
                for cls in (
                    GeocodeTool,
                    TransitTool,
                    WeatherTool,
                    WebSearchTool,
                    PreferenceTool,
                    AmapPlacesTool,
                    AmapGeocodeTool,
                    AmapRouteTool,
                    AmapLineTool,
                )
            ]
            + [PolicyTool(settings, self.cache, self.repo, self.knowledge)]
        )
        self.llm = LLMClient(settings)

    async def chat(self, request: ChatRequest, now: datetime | None = None) -> ChatResponse:
        started = monotonic()
        sid = request.session_id or self.repo.new_session()
        if not self.repo.exists(sid):
            raise KeyError("session_not_found")
        nodes = RequestNodes(self, request, sid, now or datetime.now(TZ))
        try:
            compiled = build_graph(nodes)
            remaining = nodes.budget.remaining()
            state = await asyncio.wait_for(
                compiled.ainvoke(nodes.latest, {"recursion_limit": 40}), timeout=remaining
            )
            result = state["response"]
        except (asyncio.TimeoutError, DeadlineExceeded):
            result = await nodes.fallback("planning_timeout")
        except (RuntimeError, ValueError) as exc:
            result = await nodes.fallback(type(exc).__name__)
        response = ChatResponse.model_validate(result)
        response.metadata["elapsed_ms"] = round((monotonic() - started) * 1000, 2)
        response.metadata["trace"] = nodes.latest["trace"]
        self.repo.save_turn(request.message, response)
        return response
