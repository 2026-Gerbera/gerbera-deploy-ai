"""C2 provider 변경 없이 CD 레지스트리에서 C1 검사로 연결한다."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from ddak.app import load_tools
from ddak.cd import dispatch
from ddak.cd.interface import ProviderResult
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source, Target
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.ensure_tls import EnsureTlsInput


def test_tls_tool_uses_injected_c1_check_and_propagates_failure(monkeypatch):
    tool = load_tools().get("ensure_tls")
    checker = Mock(return_value=ProviderResult(provider="aws", function="ensure_tls", passed=False))
    monkeypatch.setattr(dispatch, "_cloud_tls", checker)
    ctx = RunContext("tls-run", lock_token="tls-lock", adapter_mode=AdapterMode.REAL)
    inp = EnsureTlsInput(run_id=ctx.run_id, target=Target.CLOUD, lock_token="tls-lock")
    assert tool.fn(inp, ctx).passed is False
    checker.assert_called_once_with("check", ctx)
    checker.reset_mock()
    with pytest.raises(DdakToolError, match="LOCK_INVALID"):
        tool.fn(inp, replace(ctx, lock_token="different-lock"))
    with pytest.raises(DdakToolError, match="플랫폼 Terraform"):
        tool.fn(inp.model_copy(update={"mode": "apply"}), ctx)
    checker.assert_not_called()


def test_fake_tls_does_not_call_cloud_and_labels_source(monkeypatch):
    tool = load_tools().get("ensure_tls")
    checker = Mock(side_effect=AssertionError("external call"))
    monkeypatch.setattr(dispatch, "_cloud_tls", checker)
    ctx = RunContext("tls-run", lock_token="tls-lock")
    inp = EnsureTlsInput(run_id=ctx.run_id, target=Target.CLOUD, lock_token="tls-lock")
    assert tool.fn(inp, ctx).source is Source.FIXTURE
    checker.assert_not_called()
