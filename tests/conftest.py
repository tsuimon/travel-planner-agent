"""Isolated persistence and deterministic request clocks."""

from datetime import datetime, timedelta
import pytest
from src.agent.graph import TravelService
from src.config import Settings
from src.domain import Constraints, TZ


@pytest.fixture
def now():
    return datetime(2026, 9, 14, 10, tzinfo=TZ)


@pytest.fixture
def service(tmp_path):
    instance = TravelService(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path}/test.db",
            chroma_path=str(tmp_path / "chroma"),
            enable_ui=False,
            tool_backoff=0,
            # Existing parser/search regressions exercise the offline rule path.
            # Model-led conversation tests explicitly enable the new controller.
            conversation_agent=False,
        )
    )
    yield instance
    instance.repo.engine.dispose()


@pytest.fixture
def night(now):
    start = now.replace(hour=22, minute=30)
    return Constraints(
        origin="night_start",
        destination="night_end",
        depart_after=start,
        depart_before=start + timedelta(minutes=30),
        arrive_by=start + timedelta(hours=4),
        budget_cents=10000,
        cycling_acceptance=2,
    )
