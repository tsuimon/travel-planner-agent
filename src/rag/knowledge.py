"""Chroma + a deterministic LangChain Embeddings implementation, no model download.

Character n-grams provide a reproducible lexical baseline, not semantic embeddings.
Topic gates prevent a railway policy from answering aviation battery questions.
"""

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_core.embeddings import Embeddings


class NgramEmbeddings(Embeddings):
    dimensions = 512

    def embed_query(self, text: str) -> list[float]:
        text = re.sub(r"\s", "", text.lower())
        vec = [0.0] * self.dimensions
        for size in (1, 2, 3):
            for i in range(max(0, len(text) - size + 1)):
                idx = (
                    int.from_bytes(hashlib.sha256(text[i : i + size].encode()).digest()[:4], "big")
                    % self.dimensions
                )
                vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1
        return [x / norm for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def topic(question: str) -> str | None:
    flight = any(x in question for x in ("飞机", "航空", "机票"))
    rail = any(x in question for x in ("火车", "铁路", "高铁", "车票"))
    if any(x in question for x in ("充电宝", "锂电池")):
        if not flight and not rail:
            return None
        return "flight_battery" if flight else "rail_battery"
    if any(x in question for x in ("退票", "退改签", "改签")):
        return "flight_refund" if flight else "rail_refund"
    if any(x in question for x in ("单车", "停放", "服务区")):
        return "bike"
    if any(x in question for x in ("安检", "危险品", "烟花")):
        return "flight_security" if flight else "rail_security"
    return None


class KnowledgeBase:
    def __init__(self, path: str) -> None:
        self.embedding = NgramEmbeddings()
        self.client = chromadb.PersistentClient(
            path=path, settings=ChromaSettings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            "travel-policies-ngram-v1", embedding_function=None
        )
        self.documents = json.loads(
            (Path(__file__).parent / "documents/policies.json").read_text(encoding="utf-8")
        )
        self.build()

    def build(self) -> int:
        rows: list[dict] = []
        for doc in self.documents:
            # Deterministic overlapping chunks allow longer curated documents later.
            for offset in range(0, len(doc["content"]), 400):
                rows.append(
                    {
                        **doc,
                        "chunk_id": f"{doc['id']}:{offset}",
                        "text": doc["content"][offset : offset + 480],
                    }
                )
        current = set(self.collection.get()["ids"])
        removed = current - {r["chunk_id"] for r in rows}
        if removed:
            self.collection.delete(ids=sorted(removed))
        self.collection.upsert(
            ids=[r["chunk_id"] for r in rows],
            documents=[r["text"] for r in rows],
            embeddings=self.embedding.embed_documents([r["title"] + r["text"] for r in rows]),
            metadatas=[
                {k: r[k] for k in ("title", "topic", "url", "effective", "reviewed", "official")}
                for r in rows
            ],
        )
        return len(rows)

    def answer(self, question: str, today: date | None = None) -> tuple[str, list[dict]]:
        selected = topic(question)
        if selected is None:
            return "知识库没有足够相关的政策依据，请补充交通方式和具体问题。", []
        result = self.collection.query(
            query_embeddings=[self.embedding.embed_query(question)],
            n_results=3,
            where={"topic": selected},
            include=["documents", "metadatas", "distances"],
        )
        if not result["ids"][0]:
            return "知识库尚未收录该交通方式的对应政策，请查询承运人或主管部门官方说明。", []
        sources = []
        parts = []
        for text, meta, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            stale = ((today or date.today()) - date.fromisoformat(meta["reviewed"])).days > 180
            sources.append({**meta, "distance": distance, "stale": stale})
            parts.append(text)
            if meta["url"]:
                parts.append(
                    f"来源：[{meta['title']}]({meta['url']})；施行：{meta['effective']}；复核：{meta['reviewed']}。"
                )
            else:
                parts.append("来源：项目覆盖说明（非官方政策）。")
            if stale:
                parts.append("此条目超过180天未复核，不能确认仍然有效。")
        return "\n\n".join(parts), sources
