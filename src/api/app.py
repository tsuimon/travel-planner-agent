"""Local-user FastAPI application with optional bearer protection."""

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from src.agent.graph import TravelService
from src.config import Settings
from src.domain import ChatRequest, ChatResponse, Preferences
from src.logging_config import configure_logging


def create_app(settings: Settings | None = None, service: TravelService | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        app.state.service = service or TravelService(settings)
        yield
        app.state.service.repo.engine.dispose()

    app = FastAPI(title="多约束混合交通出行规划", version="0.1.0", lifespan=lifespan)

    async def authorize(authorization: str = Header(default="")) -> None:
        token = settings.api_access_token.get_secret_value()
        if token and not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "invalid_access_token")

    def svc() -> TravelService:
        return app.state.service

    @app.get("/health")
    async def health():
        return {"status": "ok", "data_mode": settings.data_mode, "version": "0.1.0"}

    @app.post("/api/v1/chat", response_model=ChatResponse, dependencies=[Depends(authorize)])
    async def chat(request: ChatRequest, current: TravelService = Depends(svc)):
        try:
            return await current.chat(request)
        except KeyError:
            raise HTTPException(404, "session_not_found") from None

    @app.post("/api/v1/sessions", dependencies=[Depends(authorize)])
    async def new_session(current: TravelService = Depends(svc)):
        return {"session_id": current.repo.new_session()}

    @app.get("/api/v1/sessions", dependencies=[Depends(authorize)])
    async def sessions(current: TravelService = Depends(svc)):
        return current.repo.sessions()

    @app.get("/api/v1/sessions/{sid}", dependencies=[Depends(authorize)])
    async def history(sid: str, current: TravelService = Depends(svc)):
        if not current.repo.exists(sid):
            raise HTTPException(404, "session_not_found")
        return {"session_id": sid, "messages": current.repo.history(sid, 100)}

    @app.delete("/api/v1/sessions/{sid}", dependencies=[Depends(authorize)])
    async def delete_session(sid: str, current: TravelService = Depends(svc)):
        if not current.repo.exists(sid):
            raise HTTPException(404, "session_not_found")
        current.repo.delete_session(sid)
        return {"deleted": True}

    @app.get("/api/v1/preferences", response_model=Preferences, dependencies=[Depends(authorize)])
    async def preferences(current: TravelService = Depends(svc)):
        return current.repo.preferences()

    @app.put("/api/v1/preferences", response_model=Preferences, dependencies=[Depends(authorize)])
    async def save_preferences(value: Preferences, current: TravelService = Depends(svc)):
        current.repo.save_preferences(value)
        return value

    @app.delete("/api/v1/preferences", dependencies=[Depends(authorize)])
    async def reset_preferences(current: TravelService = Depends(svc)):
        current.repo.save_preferences(Preferences())
        return {"reset": True}

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(
            "/ui" if settings.enable_ui and not settings.api_access_token.get_secret_value() else "/docs"
        )

    # UI is local-only. With token protection enabled, avoid exposing unauthenticated callbacks.
    if settings.enable_ui and not settings.api_access_token.get_secret_value():
        import gradio as gr
        from frontend.app import build_ui
        from frontend.presentation import CSS_PATH, THEME

        app = gr.mount_gradio_app(
            app,
            build_ui(lambda: app.state.service),
            path="/ui",
            theme=THEME,
            css=CSS_PATH.read_text(encoding="utf-8"),
            footer_links=[],
        )
    return app


app = create_app()
