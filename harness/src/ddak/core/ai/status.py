"""관리 페이지 "LLM 연결" 카드(✅ 장부 7). 관리 페이지를 열 때 backend와 로그인·키 상태를 보여준다.

AI를 호출하지 않는다(상태 조회만). ddak.app이 이 함수를 관리 웹에 주입한다(웹은 ddak.core.ai를
import하지 않는다). 키 값·이메일은 절대 돌려주지 않는다.
"""

from __future__ import annotations

from typing import Any

from ddak.core.config import Settings
from ddak.core.contracts.enums import LLMBackend


def llm_status(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or Settings.from_env()
    if cfg.llm_backend is LLMBackend.CLI:
        from ddak.core.ai.providers.cli import cli_status

        status = cli_status(cfg.claude_bin)
    elif cfg.llm_backend is LLMBackend.API:
        # TODO(O2): 가벼운 ping 호출로 키 유효성 확인
        status = {
            "backend": "api",
            "ok": bool(cfg.llm_api_key and cfg.llm_model),
            "detail": "키 있음(값은 표시하지 않음)" if cfg.llm_api_key else "DDAK_LLM_API_KEY 없음",
        }
    else:
        status = {"backend": "replay", "ok": True, "detail": "저장된 응답 모드(결과에 라벨 표시)"}
    status["model"] = cfg.llm_model
    status["jev_key"] = bool(cfg.jev_api_key)
    return status
