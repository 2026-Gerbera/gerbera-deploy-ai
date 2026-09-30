"""FastAPI 라우터가 사용하는 내부 서비스와 템플릿."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ddak.executor.service import DeploymentService

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")


def deployment(request: Request) -> DeploymentService:
    service = request.app.state.deployment
    if service is None:
        raise RuntimeError("배포 서비스가 연결되지 않았다")
    return service
