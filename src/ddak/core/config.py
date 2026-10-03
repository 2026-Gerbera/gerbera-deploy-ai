"""설정 로딩 한 곳. DDAK_* 환경변수와 deploy.yaml.

키 목록은 env.example(값 없음)에 있다. deploy.yaml 키 자체는 계약 문서가 정한다(TODO(contract)).
비밀값(API 키)은 repr에 나오지 않게 둔다. 로그·이벤트·AI 입력에 넣지 않는다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml

from ddak.core.contracts.enums import LLMBackend
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import IMAGE_REPOSITORY_PATTERN
from ddak.core.defaults import load_defaults
from ddak.core.logging import get_logger


@cache
def _warn_reserved_jev_key() -> None:
    get_logger("config").warning(
        "DDAK_JEV_API_KEY는 TypeSafe 전용 예약 키이며 Groq에 사용하지 않는다"
    )


# 선택 제한은 AI 패키지를 import하지 않는다(웹 -> config의 AI 경계 유지).
# trusted registry 등록 시 CLI 종류도 여기에 반영한다.
_PROVIDER_KINDS: dict[str, str] = {
    "claude-cli": "cli",
    "claude-api": "api",
    "groq": "api",
    "replay": "api",
    "jev": "api",
}


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
    llm_provider: str | None = None
    judgment_provider: str | None = None
    judgment_model: str | None = None
    anthropic_api_key: str | None = field(default=None, repr=False)
    provider_keys: Mapping[str, str] = field(default_factory=dict, repr=False, hash=False)
    llm_model: str | None = None  # 💭 실측으로 선택. 모델 ID와 prompt 버전은 고정한다
    llm_effort: Literal["low", "medium"] = "low"
    claude_bin: str = "claude"
    llm_api_key: str | None = field(default=None, repr=False)  # api backend만
    ai_timeout_s: float = 20.0
    ai_retries: int = 1
    ai_replay_dir: Path = Path("fixtures/ai_replay")
    jev_backend: Literal["groq", "claude-cli"] = "groq"
    groq_api_key: str | None = field(default=None, repr=False)
    groq_model: str | None = None
    groq_timeout_s: float = 2.0
    jev_key_configured: bool = False  # 예약 키의 존재만 확인; 값은 읽지 않는다.
    jev_api_key: str | None = field(default=None, repr=False)
    jev_model: str = "jev-1.13.0"  # 버전 고정(jev-latest 쓰지 않음)
    jev_timeout_s: float = 2.0  # SDK 기본(10초 + 재시도 2회)을 줄인다
    build_backend: Literal["codebuild", "local"] = "codebuild"
    image_repository: str | None = None
    setting_sources: Mapping[str, str] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_keys", MappingProxyType(dict(self.provider_keys)))
        object.__setattr__(self, "setting_sources", MappingProxyType(dict(self.setting_sources)))
        if self.llm_provider is not None:
            backend = (
                LLMBackend.REPLAY
                if self.llm_provider == "replay"
                else LLMBackend.CLI
                if _PROVIDER_KINDS.get(self.llm_provider) == "cli"
                else LLMBackend.API
            )
            object.__setattr__(self, "llm_backend", backend)
        if self.llm_effort not in ("low", "medium"):
            raise ValueError("DDAK_LLM_EFFORT는 low/medium만 허용한다")
        if (
            self.llm_provider == "claude-cli"
            or (self.llm_provider is None and self.llm_backend is LLMBackend.CLI)
        ) and self.llm_model is None:
            object.__setattr__(self, "llm_model", "claude-sonnet-5-5")
        if self.jev_backend not in ("groq", "claude-cli"):
            raise ValueError("DDAK_JEV_BACKEND는 groq/claude-cli만 허용한다")
        if self.build_backend not in ("codebuild", "local"):
            raise ValueError("DDAK_BUILD_BACKEND는 codebuild/local만 허용한다")
        if self.image_repository is not None and not re.fullmatch(
            IMAGE_REPOSITORY_PATTERN, self.image_repository
        ):
            raise ValueError("DDAK_IMAGE_REPOSITORY는 namespace/repository 형식이어야 한다")
        # make 진입점은 harness를 cwd로 쓴다. 설정을 읽는 시점에 기준을 고정하여
        # 이후 Git checkout의 cwd나 컨트롤러 재시작 위치에 영향을 받지 않게 한다.
        for name in ("deploy_config", "run_dir", "ai_replay_dir"):
            object.__setattr__(self, name, getattr(self, name).expanduser().resolve())

    def selected_provider(self, role: Literal["generation", "judgment"]) -> str:
        if role == "judgment":
            return self.judgment_provider or self.jev_backend
        return (
            self.llm_provider
            or {LLMBackend.CLI: "claude-cli", LLMBackend.API: "groq", LLMBackend.REPLAY: "replay"}[
                self.llm_backend
            ]
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        real = env.get("DDAK_ADAPTER_MODE") == AdapterMode.REAL.value
        defaults = load_defaults() if real else {}
        judgment = env.get("DDAK_JEV_BACKEND") or defaults.get("judgment_provider", "groq")
        selected_judgment = env.get("DDAK_JUDGMENT_PROVIDER") or judgment
        selected_generation = env.get("DDAK_LLM_PROVIDER") or (
            defaults.get("generation_provider") if not env.get("DDAK_LLM_BACKEND") else None
        )
        source_keys = {
            "generation_provider": ("DDAK_LLM_PROVIDER", "DDAK_LLM_BACKEND"),
            "generation_model": ("DDAK_LLM_MODEL",),
            "judgment_provider": ("DDAK_JUDGMENT_PROVIDER", "DDAK_JEV_BACKEND"),
            "judgment_model": ("DDAK_JUDGMENT_MODEL",),
            "llm_effort": ("DDAK_LLM_EFFORT",),
            "ai_timeout_s": ("DDAK_AI_TIMEOUT_S",),
            "build_backend": ("DDAK_BUILD_BACKEND",),
            "image_repository": ("DDAK_IMAGE_REPOSITORY",),
        }
        groq_key = (
            env.get("DDAK_GROQ_API_KEY")
            if (selected_judgment == "groq" or selected_generation == "groq")
            else None
        )
        if selected_judgment == "groq" and not groq_key and "DDAK_JEV_API_KEY" in env:
            _warn_reserved_jev_key()
        return cls(
            adapter_mode=AdapterMode(env.get("DDAK_ADAPTER_MODE", AdapterMode.FAKE.value)),
            deploy_config=Path(env.get("DDAK_DEPLOY_CONFIG", "fixtures/deploy.yaml")),
            run_dir=Path(env.get("DDAK_RUN_DIR", "var/runs")),
            log_level=env.get("DDAK_LOG_LEVEL", "INFO"),
            admin_port=int(env.get("DDAK_ADMIN_PORT", "8765")),
            llm_backend=LLMBackend(env.get("DDAK_LLM_BACKEND") or LLMBackend.REPLAY.value),
            llm_provider=selected_generation,
            judgment_provider=env.get("DDAK_JUDGMENT_PROVIDER") or None,
            judgment_model=env.get("DDAK_JUDGMENT_MODEL")
            or (defaults.get("judgment_model") if selected_judgment == "claude-cli" else None),
            anthropic_api_key=env.get("DDAK_ANTHROPIC_API_KEY") or None,
            llm_model=env.get("DDAK_LLM_MODEL")
            or (defaults.get("generation_model") if selected_generation == "claude-cli" else None),
            llm_effort=cast(
                Literal["low", "medium"],
                env.get("DDAK_LLM_EFFORT") or defaults.get("llm_effort", "low"),
            ),
            claude_bin=env.get("DDAK_CLAUDE_BIN") or "claude",
            llm_api_key=env.get("DDAK_LLM_API_KEY") or None,
            ai_timeout_s=float(env.get("DDAK_AI_TIMEOUT_S") or defaults.get("ai_timeout_s", 20)),
            ai_retries=int(env.get("DDAK_AI_RETRIES") or "1"),
            ai_replay_dir=Path(env.get("DDAK_AI_REPLAY_DIR") or "fixtures/ai_replay"),
            jev_backend=cast(Literal["groq", "claude-cli"], judgment),
            groq_api_key=groq_key or None,
            groq_model=env.get("DDAK_GROQ_MODEL") or None,
            groq_timeout_s=float(env.get("DDAK_GROQ_TIMEOUT_S") or "2"),
            jev_key_configured="DDAK_JEV_API_KEY" in env,
            jev_model=env.get("DDAK_JEV_MODEL") or "jev-1.13.0",
            jev_timeout_s=float(env.get("DDAK_JEV_TIMEOUT_S") or "2"),
            build_backend=cast(
                Literal["codebuild", "local"],
                env.get("DDAK_BUILD_BACKEND") or defaults.get("build_backend", "codebuild"),
            ),
            image_repository=env.get("DDAK_IMAGE_REPOSITORY") or defaults.get("image_repository"),
            setting_sources={
                key: "실행환경"
                if any(env.get(var) for var in variables)
                else "기본 파일"
                if key in defaults
                else "실행환경"
                for key, variables in source_keys.items()
            },
        )


def require_local_cli(
    settings: Settings, *, host: str | None, environ: Mapping[str, str] | None = None
) -> None:
    """CLI는 호스트의 로컬 진입점에서만. 표식 없는 원격 서버를 판별하는 기능은 아니다."""
    kinds = [
        _PROVIDER_KINDS.get(settings.selected_provider(role)) for role in ("generation", "judgment")
    ]
    if None in kinds:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "등록되지 않은 AI provider 설정")
    if "cli" not in kinds:
        return
    env = os.environ if environ is None else environ
    try:
        loopback = host is not None and ip_address(host).is_loopback
    except ValueError:
        loopback = False  # 호스트명 해석/외부 DNS에 기대지 않는다.
    remote = any(
        env.get(key)
        for key in (
            "SSH_CONNECTION",
            "SSH_CLIENT",
            "SSH_TTY",
            "container",
            "KUBERNETES_SERVICE_HOST",
            "ECS_CONTAINER_METADATA_URI",
            "ECS_CONTAINER_METADATA_URI_V4",
            "AWS_EXECUTION_ENV",
        )
    )
    in_container = any(Path(p).exists() for p in ("/.dockerenv", "/run/.containerenv"))
    in_ci = env.get("CI", "").lower() not in ("", "0", "false", "no")
    if not loopback or remote or in_container or in_ci:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "cli backend는 로컬 터미널의 loopback 관리 웹에서만 사용한다; make -C harness run 사용",
        )


def load_deploy_config(path: Path) -> dict[str, Any]:
    """deploy.yaml을 dict로 읽는다. TODO(contract): DeployConfig 모델로 검증해서 돌려준다."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"deploy.yaml 최상위는 매핑이어야 한다: {path.name}")
    return data
