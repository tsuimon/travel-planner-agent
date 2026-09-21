"""Write inspectable synthetic data; this is not live transit information."""

from datetime import datetime
from pathlib import Path
from src.domain import TZ
from src.search.sample import sample_network

if __name__ == "__main__":
    target = Path("data/sample-network.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(sample_network(datetime.now(TZ)).model_dump_json(indent=2), encoding="utf-8")
    print(target.resolve())
