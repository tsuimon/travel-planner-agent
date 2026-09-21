"""Small transactional repository for the local user and their sessions."""

from pathlib import Path
from uuid import uuid4
import time

from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.domain import ChatResponse, Preferences
from src.data.models import Base, CacheRecord, Conversation, Message, PlanRecord, Preference, User


class Repository:
    def __init__(self, url: str) -> None:
        database = make_url(url).database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"poolclass": StaticPool} if database in (None, "", ":memory:") else {}
        self.engine = create_engine(url, connect_args={"check_same_thread": False}, **kwargs)

        @event.listens_for(self.engine, "connect")
        def configure(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")

        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db, db.begin():
            if db.get(User, "local") is None:
                db.add(User(id="local"))

    def preferences(self) -> Preferences:
        with Session(self.engine) as db:
            row = db.get(Preference, "local")
            return Preferences.model_validate(row.value) if row else Preferences()

    def save_preferences(self, value: Preferences) -> None:
        with Session(self.engine) as db, db.begin():
            db.merge(Preference(user_id="local", value=value.model_dump(mode="json")))

    def new_session(self) -> str:
        sid = uuid4().hex
        with Session(self.engine) as db, db.begin():
            db.add(Conversation(id=sid, user_id="local"))
        return sid

    def exists(self, sid: str) -> bool:
        with Session(self.engine) as db:
            return db.get(Conversation, sid) is not None

    def sessions(self) -> list[dict]:
        with Session(self.engine) as db:
            return [
                {"id": r.id, "created_at": r.created_at.isoformat()}
                for r in db.scalars(select(Conversation).order_by(Conversation.created_at.desc()).limit(100))
            ]

    def history(self, sid: str, limit: int = 20) -> list[dict]:
        with Session(self.engine) as db:
            rows = list(
                db.scalars(
                    select(Message).where(Message.session_id == sid).order_by(Message.id.desc()).limit(limit)
                )
            )
            return [{"role": r.role, "content": r.content, "payload": r.payload} for r in reversed(rows)]

    def save_turn(self, text: str, response: ChatResponse) -> None:
        with Session(self.engine) as db, db.begin():
            db.add(Message(session_id=response.session_id, role="user", content=text))
            db.add(
                Message(
                    session_id=response.session_id,
                    role="assistant",
                    content=response.answer,
                    payload=response.model_dump(mode="json"),
                )
            )
            db.add_all(
                [
                    PlanRecord(session_id=response.session_id, value=p.model_dump(mode="json"))
                    for p in response.plans
                ]
            )

    def delete_session(self, sid: str) -> None:
        with Session(self.engine) as db, db.begin():
            db.execute(delete(Conversation).where(Conversation.id == sid))

    def cache_get(self, key: str) -> dict | None:
        with Session(self.engine) as db, db.begin():
            row = db.get(CacheRecord, key)
            if row and row.expires_at > time.time():
                return row.value
            if row:
                db.delete(row)
        return None

    def cache_set(self, key: str, value: dict, ttl: float) -> None:
        with Session(self.engine) as db, db.begin():
            db.merge(CacheRecord(key=key, value=value, expires_at=time.time() + ttl))
