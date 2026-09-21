"""Build the deterministic offline Chroma policy index."""

from src.config import Settings
from src.rag.knowledge import KnowledgeBase

if __name__ == "__main__":
    kb = KnowledgeBase(Settings().chroma_path)
    print("Indexed chunks:", kb.collection.count())
