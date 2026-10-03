"""설정 로딩 한 곳. DDAK_* 환경변수와 deploy.yaml.

키 목록은 env.example(값 없음)에 있다. deploy.yaml 키 자체는 계약 문서가 정한다(TODO(contract)).
비밀값(API 키)은 repr에 나오지 않게 둔다. 로그·이벤트·AI 입력에 넣지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from ddak.core.contracts.enums import LLMBackend


class AdapterMode(StrEnum):
    FAKE = "fake"  # 결정적 가짜 어댑터(테스트, 드라이런, UI 개발)
    REAL = "real"  # 실제 Docker / AWS


@dataclass(frozen=True)
class Settings:
    adapter_mode: AdapterMode = AdapterMode.FAKE
    deploy_config: Path = Path("fixtures/deploy.yaml")
    run_dir: Path = Path("var/runs")
    log_level: str = "INFO"
    admin_port: int = 8765
    # ---- AI(call_ai, ✅ 장부 7) ----
    llm_backend: LLMBackend = LLMBackend.REPLAY  # 기본값은 호출이 일어나지 않는 replay
    llm_model: str | None = "claude-sonnet-5-5"
    claude_bin: str = "claude"
    llm_api_key: str | None = field(default=None, repr=False)  # api backend만
    ai_timeout_s: float = 240.0
    ai_retries: int = 1
    ai_replay_dir: Path = Path("fixtures/ai_replay")
    jev_api_key: str | None = field(default=None, repr=False)
    jev_model: str = "jev-1.13.0"  # 버전 고정(jev-latest 쓰지 않음)
    jev_timeout_s: float = 2.0  # SDK 기본(10초 + 재시도 2회)을 줄인다

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        return cls(
            adapter_mode=AdapterMode(env.get("DDAK_ADAPTER_MODE", AdapterMode.FAKE.value)),
            deploy_config=Path(env.get("DDAK_DEPLOY_CONFIG", "fixtures/deploy.yaml")),
            run_dir=Path(env.get("DDAK_RUN_DIR", "var/runs")),
            log_level=env.get("DDAK_LOG_LEVEL", "INFO"),
            admin_port=int(env.get("DDAK_ADMIN_PORT", "8765")),
            llm_backend=LLMBackend(env.get("DDAK_LLM_BACKEND") or LLMBackend.REPLAY.value),
            llm_model=env.get("DDAK_LLM_MODEL") or "claude-sonnet-5-5",
            claude_bin=env.get("DDAK_CLAUDE_BIN") or "claude",
            llm_api_key=env.get("DDAK_LLM_API_KEY") or None,
            ai_timeout_s=float(env.get("DDAK_AI_TIMEOUT_S") or "240"),
            ai_retries=int(env.get("DDAK_AI_RETRIES") or "1"),
            ai_replay_dir=Path(env.get("DDAK_AI_REPLAY_DIR") or "fixtures/ai_replay"),
            jev_api_key=env.get("DDAK_JEV_API_KEY") or None,
            jev_model=env.get("DDAK_JEV_MODEL") or "jev-1.13.0",
            jev_timeout_s=float(env.get("DDAK_JEV_TIMEOUT_S") or "2"),
        )


def load_deploy_config(path: Path) -> dict[str, Any]:
    """deploy.yaml을 dict로 읽는다. TODO(contract): DeployConfig 모델로 검증해서 돌려준다."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"deploy.yaml 최상위는 매핑이어야 한다: {path.name}")
    return data
