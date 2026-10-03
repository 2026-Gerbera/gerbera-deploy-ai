"""코드가 선택하는 작은 provider registry. 관리 테스트에는 사용자 프롬프트 인자가 없다."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ddak.core.ai.providers.jev import JudgmentClient
from ddak.core.ai.providers.types import AIResponse, LLMProvider
from ddak.core.config import _PROVIDER_KINDS, Settings
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact

ProviderRole = Literal["generation", "judgment"]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: Literal["cli", "api"]
    roles: tuple[ProviderRole, ...] = ("generation", "judgment")
    auth: str = "none"
    key_name: str | None = None  # Settings 필드명, 환경변수/비밀값 아님
    default_model: str | None = None
    models: tuple[str, ...] = ()
    generation_factory: Callable[[Settings], LLMProvider] | None = None
    judgment_factory: Callable[[Settings], JudgmentClient] | None = None
    status: Callable[[Settings], Mapping[str, Any]] | None = None
    test: Callable[[AIResponse], bool] | None = None  # 공통 fixed probe 이후 추가 검증만


_REGISTRY: dict[str, ProviderSpec] = {}
_VERIFIED: dict[tuple[str, ProviderRole, str], dict[str, Any]] = {}


def register_provider(spec: ProviderSpec) -> None:
    """신뢰하는 조립 코드용. 등록 1회로 생성/판단/메타/상태 모두 연결한다."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", spec.id) or spec.kind not in ("cli", "api"):
        raise ValueError("provider id/kind 오류")
    if not spec.roles or any(role not in ("generation", "judgment") for role in spec.roles):
        raise ValueError("provider roles 오류")
    if "generation" in spec.roles and spec.generation_factory is None:
        raise ValueError("generation factory 필요")
    if "judgment" in spec.roles and not (spec.judgment_factory or spec.generation_factory):
        raise ValueError("judgment factory 필요")
    if spec.key_name and (
        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", spec.key_name)
        or (
            spec.key_name in Settings.__dataclass_fields__
            and Settings.__dataclass_fields__[spec.key_name].repr is not False
        )
    ):
        raise ValueError("key_name은 provider_keys 이름 또는 비밀 Settings 필드여야 한다")
    _REGISTRY[spec.id] = spec
    _PROVIDER_KINDS[spec.id] = spec.kind
    for key in list(_VERIFIED):
        if key[0] == spec.id:
            del _VERIFIED[key]


def provider_key(settings: Settings, key_name: str | None) -> str | None:
    """명시적 주입 키 우선; 기존 비밀 필드만 fallback. 값은 metadata에 노출하지 않는다."""
    if key_name is None:
        return None
    if key_name in settings.provider_keys:
        return settings.provider_keys[key_name] or None
    field = Settings.__dataclass_fields__.get(key_name)
    if field is not None and field.repr is False:
        value = getattr(settings, key_name)
        return value if isinstance(value, str) and value else None
    return None


def _probe_key(
    spec: ProviderSpec, settings: Settings, role: ProviderRole
) -> tuple[str, ProviderRole, str]:
    credential = provider_key(settings, spec.key_name)
    if spec.id == "groq" and role == "generation":
        credential = _groq_generation_key(settings)
    payload = (
        provider_model(settings, role, id=spec.id),
        credential,
        settings.claude_bin,
        settings.llm_effort,
    )
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    return spec.id, role, digest


def get_provider_spec(id: str) -> ProviderSpec:
    try:
        return _REGISTRY[id]
    except KeyError:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "등록되지 않은 AI provider") from None


def validate_provider_selection(id: str, role: ProviderRole) -> None:
    spec = get_provider_spec(id)
    if role not in ("generation", "judgment") or role not in spec.roles:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "provider가 요청한 역할을 지원하지 않는다")


def selected_provider_id(settings: Settings, role: ProviderRole = "generation") -> str:
    id = settings.selected_provider(role)
    validate_provider_selection(id, role)
    return id


def provider_model(
    settings: Settings, role: ProviderRole = "generation", *, id: str | None = None
) -> str | None:
    spec = get_provider_spec(id or selected_provider_id(settings, role))
    if role == "generation":
        return settings.llm_model or spec.default_model
    if settings.judgment_model:
        return settings.judgment_model
    if spec.id == "groq":
        return settings.groq_model or spec.default_model
    if spec.id == "jev":
        return settings.jev_model
    if spec.id == settings.selected_provider("generation"):
        return settings.llm_model or spec.default_model
    return spec.default_model


def get_provider(settings: Settings) -> LLMProvider:
    spec = get_provider_spec(selected_provider_id(settings))
    if spec.generation_factory is None:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "generation factory 없음")
    return spec.generation_factory(settings)


def _judgment_client(spec: ProviderSpec, settings: Settings) -> JudgmentClient:
    if spec.judgment_factory is not None:
        return spec.judgment_factory(settings)
    from ddak.core.ai.providers.claude import ClaudeJevClient

    if spec.generation_factory is None:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "generation factory 없음")
    client = ClaudeJevClient(
        model=provider_model(settings, "judgment", id=spec.id),
        timeout_s=settings.ai_timeout_s,
        provider=spec.generation_factory(settings),
        default_model=spec.default_model,
    )
    client.name = spec.id
    return client


def get_jev_client(settings: Settings | None = None) -> JudgmentClient:
    cfg = settings or Settings.from_env()
    spec = get_provider_spec(selected_provider_id(cfg, "judgment"))
    return _judgment_client(spec, cfg)


def provider_catalog(settings: Settings | None = None) -> list[dict[str, Any]]:
    """公開 metadata만 반환. key_configured는 settings 제공 시에만 부울로 추가한다."""
    result = []
    for spec in _REGISTRY.values():
        item = {
            "id": spec.id,
            "label": spec.label,
            "kind": spec.kind,
            "roles": list(spec.roles),
            "auth": spec.auth,
            "key_name": spec.key_name,
            "default_model": spec.default_model,
            "models": list(spec.models),
        }
        if settings is not None:
            item["key_configured"] = bool(provider_key(settings, spec.key_name)) or (
                spec.id == "jev" and settings.jev_key_configured
            )
        result.append(item)
    return result


def _role(spec: ProviderSpec, settings: Settings, role: ProviderRole | None) -> ProviderRole:
    if role is None:
        role = (
            "judgment"
            if settings.selected_provider("judgment") == spec.id
            and settings.selected_provider("generation") != spec.id
            else spec.roles[0]
        )
    validate_provider_selection(spec.id, role)
    return role


def provider_status(
    id: str, settings: Settings, *, role: ProviderRole | None = None
) -> dict[str, Any]:
    """AI 호출 없음. green은 동일 설정의 명시적 live probe 성공 기록이 있을 때만."""
    spec = get_provider_spec(id)
    role = _role(spec, settings, role)
    key = _probe_key(spec, settings, role)
    try:
        raw = (
            spec.status(settings)
            if spec.status
            else _api_status(provider_key(settings, spec.key_name))
            if spec.key_name
            else {}
        )
    except Exception:
        raw = {"status": "red", "detail": "provider 상태 조회 실패"}
    state = "red" if raw.get("status") == "red" else "gray"
    detail = str(raw.get("detail", "연결 테스트 전"))
    credential = provider_key(settings, spec.key_name)
    if credential:
        detail = detail.replace(credential, "[REDACTED]")
    detail = redact(detail)
    detail = re.sub(r"[^\s@]+@[^\s@]+", "[REDACTED]", detail)
    if state == "red":
        _VERIFIED.pop(key, None)
    if key in _VERIFIED:
        return dict(_VERIFIED[key])
    return {"status": state, "detail": detail}


def test_provider_connection(
    id: str, settings: Settings, *, role: ProviderRole | None = None
) -> dict[str, Any]:
    """명시적 관리 연결 테스트 전용. 고정된 최소 질문만 공통 gate로 호출한다."""
    from ddak.core.ai.gateway import _test_provider_connection

    spec = get_provider_spec(id)
    role = _role(spec, settings, role)
    key = _probe_key(spec, settings, role)
    _VERIFIED.pop(key, None)
    if spec.id in ("replay", "jev"):
        return {
            "status": "gray",
            "detail": "저장 응답/미연결 provider는 live 연결을 검증하지 않는다",
        }
    try:
        response = _test_provider_connection(settings=settings, spec=spec, role=role)
        if response.source is not Source.LIVE:
            result = {"status": "gray", "detail": "live 연결 응답이 아니다"}
        elif spec.test and not spec.test(response):
            result = {"status": "red", "detail": "provider 연결 응답 검증 실패"}
        else:
            result = {
                "status": "green",
                "detail": "연결 테스트 성공",
                "verified_at": datetime.now(UTC).isoformat(),
            }
    except Exception:
        result = {"status": "red", "detail": "provider 연결 테스트 실패"}
    _VERIFIED[key] = result
    return dict(result)


def _cli(settings: Settings) -> LLMProvider:
    from ddak.core.ai.providers.cli import ClaudeCliProvider

    return ClaudeCliProvider(settings.claude_bin, effort=settings.llm_effort)


def _claude_api(settings: Settings) -> LLMProvider:
    from ddak.core.ai.providers.anthropic_api import ClaudeApiProvider

    return ClaudeApiProvider(
        provider_key(settings, "anthropic_api_key"), effort=settings.llm_effort
    )


def _groq(settings: Settings) -> LLMProvider:
    from ddak.core.ai.providers.groq_api import GroqApiProvider

    return GroqApiProvider(_groq_generation_key(settings))


def _groq_generation_key(settings: Settings) -> str | None:
    if "groq_api_key" in settings.provider_keys:
        return provider_key(settings, "groq_api_key")
    if settings.llm_provider is None and settings.llm_backend.value == "api":
        return settings.llm_api_key
    return provider_key(settings, "groq_api_key")


def _groq_judgment(settings: Settings) -> JudgmentClient:
    from ddak.core.ai.providers.jev import GroqJevClient

    return GroqJevClient(
        provider_key(settings, "groq_api_key"),
        model=provider_model(settings, "judgment", id="groq"),
        timeout_s=settings.groq_timeout_s,
    )


def _replay(settings: Settings) -> LLMProvider:
    from ddak.core.ai.providers.replay import ReplayProvider

    return ReplayProvider(settings.ai_replay_dir)


def _jev(settings: Settings) -> JudgmentClient:
    from ddak.core.ai.providers.jev import JevClient

    return JevClient(None, model=settings.jev_model, timeout_s=settings.jev_timeout_s)


def _cli_status(settings: Settings) -> Mapping[str, Any]:
    from ddak.core.ai.providers.cli import cli_status

    return cli_status(settings.claude_bin)


def _api_status(key: str | None) -> Mapping[str, Any]:
    return {
        "status": "gray" if key else "red",
        "detail": "키 설정됨; 연결 테스트 전" if key else "API 키 미설정",
    }


register_provider(
    ProviderSpec(
        id="claude-cli",
        label="Claude CLI",
        kind="cli",
        auth="local-cli",
        default_model="claude-sonnet-5-5",
        models=("claude-sonnet-5-5",),
        generation_factory=_cli,
        status=_cli_status,
    )
)
register_provider(
    ProviderSpec(
        id="claude-api",
        label="Claude API",
        kind="api",
        auth="api-key",
        key_name="anthropic_api_key",
        default_model="claude-sonnet-5-5",
        models=("claude-sonnet-5-5",),
        generation_factory=_claude_api,
        status=lambda cfg: _api_status(provider_key(cfg, "anthropic_api_key")),
    )
)
register_provider(
    ProviderSpec(
        id="groq",
        label="Groq",
        kind="api",
        auth="api-key",
        key_name="groq_api_key",
        generation_factory=_groq,
        judgment_factory=_groq_judgment,
        status=lambda cfg: _api_status(
            _groq_generation_key(cfg) or provider_key(cfg, "groq_api_key")
        ),
    )
)
register_provider(
    ProviderSpec(
        id="replay",
        label="저장 응답",
        kind="api",
        generation_factory=_replay,
        status=lambda cfg: {"status": "gray", "detail": "저장된 응답 모드(source=replay)"},
    )
)
register_provider(
    ProviderSpec(
        id="jev",
        label="TypeSafe Jev (미연결)",
        kind="api",
        roles=("judgment",),
        auth="api-key",
        key_name="jev_api_key",
        default_model="jev-1.13.0",
        judgment_factory=_jev,
        status=lambda cfg: {"status": "gray", "detail": "TypeSafe Jev 미연결"},
    )
)
