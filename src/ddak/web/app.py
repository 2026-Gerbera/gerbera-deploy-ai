"""C3 FastAPI 관리 웹. 127.0.0.1에만 바인드한다.

외부 공개는 팀 결정 뒤. 그때는 인증 필수, LLM은 API 키 backend(구독 CLI 금지).

TODO(C3/O2): 채팅. 의도 JSON은 AI 툴을 레지스트리 이름으로 불러 만든다(툴 이름·위치 결정 필요).
          웹은 LLM을 직접 부르지 않는다. 채팅이 할 수 있는 것은 조회와 "배포 요청(사람 승인)"뿐이다.
보안: Host·Origin 허용목록 + SameSite=Strict 쿠키 + POST CSRF 토큰.
          LLM 출력은 이스케이프한 텍스트로만 렌더링한다(마크다운 이미지·링크 자동 로드 금지).
          코드 수정 토글 OFF는 UI가 아니라 서버에서 강제한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from ddak.core.config import Settings
from ddak.executor.service import DeploymentService
from ddak.web.i18n import LanguageMiddleware
from ddak.web.routes import ROUTERS

LLMStatusFn = Callable[[], Mapping[str, Any]]


def create_app(
    *,
    llm_status: LLMStatusFn | None = None,
    deployment_factory: Callable[[], DeploymentService] | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    current_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        deployment = deployment_factory() if deployment_factory else None
        app.state.deployment = deployment
        try:
            yield
        finally:
            if deployment:
                await deployment.shutdown()

    app = FastAPI(title="ddak", lifespan=lifespan)
    app.add_middleware(LanguageMiddleware)
    app.state.settings = current_settings
    app.state.llm_status = llm_status
    static = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    for router in ROUTERS:
        app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/llm-status")
    async def llm_status_view() -> dict[str, Any]:
        if llm_status is None:
            return {"backend": "unknown", "ok": False, "detail": "상태 함수 미주입"}
        return dict(llm_status())

    @app.get("/events")
    async def events(run_id: str) -> None:
        """TODO(C3): fastapi.sse.EventSourceResponse로 RunEvent를 흘려보낸다.

        2열이라도 스트림 하나 + event 타입으로 나눈다(브라우저 동시 연결 6개 제한).
        """
        del run_id
        raise HTTPException(status_code=501, detail="TODO(C3): SSE 미구현")

    @app.post("/api/chat")
    async def chat() -> None:
        """TODO(C3/O2): 채팅.

        EventSource는 GET만 되므로 채팅 스트림은 fetch + ReadableStream으로 받는다.
        """
        raise HTTPException(status_code=501, detail="TODO(C3/O2): 채팅 미구현")

    return app
