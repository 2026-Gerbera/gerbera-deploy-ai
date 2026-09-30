"""@tool 데코레이터 등록 규칙: 이름, 위치(모듈), 시그니처, 중복.

테스트용 레지스트리를 따로 만든다.
"""

from __future__ import annotations

import pytest

from ddak.core.contracts.base import ContractModel, ToolInput
from ddak.core.contracts.context import RunContext
from ddak.core.registry import CATALOG, Registry, RegistryError, UnknownToolError


class _In(ToolInput):
    pass


class _Out(ContractModel):
    ok: bool


def _fresh(package: str | None = None) -> Registry:
    return Registry(CATALOG, package=package)


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(RegistryError, match="카탈로그에 없는"):
        _fresh().tool("no_such_tool")


def test_library_and_executor_functions_are_not_registrable() -> None:
    with pytest.raises(RegistryError, match="등록 함수가 아니다"):
        _fresh().tool("call_ai")
    with pytest.raises(RegistryError, match="등록 함수가 아니다"):
        _fresh().tool("request_approval")


def test_tool_must_live_in_its_module() -> None:
    # 이 함수는 tests 모듈에 있으므로 ddak.cd. 아래가 아니다.
    registry = _fresh(package="ddak")
    with pytest.raises(RegistryError, match="cd 모듈 소속"):

        @registry.tool("deploy_tier")
        def deploy_tier(inp: _In, ctx: RunContext) -> _Out:
            return _Out(ok=True)


def test_signature_must_be_inp_ctx() -> None:
    registry = _fresh()
    with pytest.raises(RegistryError, match=r"\(inp, ctx\)"):

        @registry.tool("detect_changed_tiers")
        def bad(run_id: str) -> _Out:
            return _Out(ok=True)


def test_signature_must_use_contract_models() -> None:
    registry = _fresh()
    with pytest.raises(RegistryError, match="반환 타입"):

        @registry.tool("detect_changed_tiers")
        def bad(inp: _In, ctx: RunContext) -> dict:
            return {}


def test_registration_tracks_missing_and_rejects_duplicates() -> None:
    registry = _fresh()

    @registry.tool("detect_changed_tiers")
    async def detect_changed_tiers(inp: _In, ctx: RunContext) -> _Out:
        return _Out(ok=True)

    assert "detect_changed_tiers" in registry.registered()
    assert "generate_plan" in registry.missing()
    assert registry.get("detect_changed_tiers").is_async is True
    with pytest.raises(RegistryError, match="두 번"):

        @registry.tool("detect_changed_tiers")
        def again(inp: _In, ctx: RunContext) -> _Out:
            return _Out(ok=True)


def test_get_unregistered_raises() -> None:
    with pytest.raises(UnknownToolError):
        _fresh().get("deploy_tier")
