"""조립 진입점: 툴 모듈을 레지스트리에 채우고 관리 웹을 만든다. 모든 툴 모듈을 import하는 유일한 곳.

- 개발·데모: 호스트에서 `make run`(= uv run python -m ddak). 127.0.0.1에만 바인드(✅ 장부 11).
  로그인해 둔 Claude CLI, 호스트 docker, ~/.aws 프로필을 그대로 쓴다.
- 배포·공유용 컨테이너 이미지 1개(docker run 한 줄)는 TODO(O1).
  💭 컨테이너에서는 웹(관리 화면·계획)과 실행기(배포 툴, docker·AWS 권한)를 두 컨테이너로
  나누고, docker.sock 직접 마운트 대신 socket proxy 또는 DOCKER_HOST=ssh://를 쓰는 안을 검토한다.
- ddak.core.ai는 LLM 상태 함수를 웹에 주입하려고만 import한다(호출하지 않는다).
"""

from __future__ import annotations

import importlib

from fastapi import FastAPI

from ddak.core.ai.status import llm_status
from ddak.core.config import Settings
from ddak.core.registry import REGISTRY, Registry, import_tools
from ddak.executor.service import DeploymentService
from ddak.web.app import create_app

# 이 패키지들 바로 아래 <디렉토리>/tool.py를 자동 탐색한다(tool.py가 없는 디렉토리는 건너뜀).
TOOL_PACKAGES = (
    "ddak.core.tools",
    "ddak.plan",  # plan/<intake|detect|analyze|planner|validate|patch|dockerfile>/tool.py
    "ddak.cloud.infra.tools",
    "ddak.cloud.build.tools",
    "ddak.cloud",  # cloud/<deploy|tls|health>/tool.py (verify_tls는 cloud/health)
    "ddak.cd.tools",
    "ddak.verify",  # verify/<smoke|compare|diagnose|report>/tool.py
    "ddak.ops.tools",
)


def load_tools() -> Registry:
    """TOOL_PACKAGES를 자동 탐색해 등록한다. 여러 번 불러도 같은 결과다(모듈 캐시)."""
    for name in TOOL_PACKAGES:
        import_tools(importlib.import_module(name))
    return REGISTRY


def create() -> FastAPI:
    registry = load_tools()
    settings = Settings.from_env()
    return create_app(
        llm_status=llm_status,
        deployment_factory=lambda: DeploymentService(registry, settings.run_dir.parent),
    )


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    # 외부에 열지 않는다(127.0.0.1). 온프렘 앱 기본 주소 8080과 겹치지 않게 8765.
    # 포트는 💭(설계 문서 00 N21, 하네스 I-26).
    uvicorn.run(create(), host="127.0.0.1", port=settings.admin_port)
