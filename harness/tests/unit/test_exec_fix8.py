"""후속8: 네트워크 없는 AI fallback·선택 툴 필터·로컬 CLI 시작 경계."""

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from ddak import app
from ddak.core.config import Settings, require_local_cli
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import LLMBackend
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.plan.validate import validate_plan
from tests.unit.plan.validate.test_validate import draft, facts, ids

assembly = importlib.import_module("ddak.plan.validate.assemble")


@pytest.mark.parametrize("strict", [True, False])
def test_unregistered_optional_dropped_with_warning_and_invalidation(strict):
    choices = ("deploy.storage.local", "deploy.storage.cloud", "verify.watch.cloud")
    p = validate_plan(
        ValidatePlanInput(
            run_id="run-1", facts=facts(), draft=draft(*((i, True) for i in choices))
        ),
        RunContext("run-1", toggles={"strict_ai_check": strict}),
        registered_tools=set(),
    )
    assert not set(choices) & ids(p)
    assert {i.id for i in p.invalidated if i.result == "forced_skip"} == set(choices)
    assert len([w for w in p.warnings if w.code == "unregistered_optional_tool"]) == 3
    assert all("미등록" in w.message for w in p.warnings)
    assert {"build.was", "verify.health.local", "deploy.migrate.local"} <= ids(p)


def test_optional_registration_checked_at_each_assembly(monkeypatch):
    registered = set()
    monkeypatch.setattr(assembly, "REGISTRY", Mock(registered=lambda: registered))
    inp = ValidatePlanInput(
        run_id="run-1", facts=facts(), draft=draft(("verify.watch.cloud", True))
    )
    assert "verify.watch.cloud" not in ids(validate_plan(inp, RunContext("run-1")))
    registered.add("watch_post_deploy")
    p = validate_plan(inp, RunContext("run-1"))
    assert "verify.watch.cloud" in ids(p)
    assert not any("watch_post_deploy" in w.message for w in p.warnings)


CLI = Settings(llm_backend=LLMBackend.CLI)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_cli_local_loopback_allowed(monkeypatch, host):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    require_local_cli(CLI, host=host, environ={"WSL_DISTRO_NAME": "fixture"})


@pytest.mark.parametrize("host", [None, "0.0.0.0", "::", "192.0.2.1", "localhost"])  # noqa: S104
def test_cli_unknown_or_non_loopback_binding_denied(monkeypatch, host):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(DdakToolError) as exc:
        require_local_cli(CLI, host=host, environ={})
    assert exc.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    "key",
    [
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "container",
        "KUBERNETES_SERVICE_HOST",
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
        "AWS_EXECUTION_ENV",
        "INVOCATION_ID",
        "CI",
    ],
)
def test_cli_remote_container_ci_denied(monkeypatch, key):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(DdakToolError):
        require_local_cli(CLI, host="127.0.0.1", environ={key: "fixture"})


@pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
def test_cli_container_file_denied(monkeypatch, marker):
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == marker)
    with pytest.raises(DdakToolError):
        require_local_cli(CLI, host="127.0.0.1", environ={})


@pytest.mark.parametrize("backend", [LLMBackend.API, LLMBackend.REPLAY])
def test_guard_does_not_restrict_other_backends(backend):
    require_local_cli(Settings(llm_backend=backend), host=None, environ={"container": "fixture"})


def test_cli_factory_without_binding_refuses_before_initialization(monkeypatch):
    monkeypatch.setenv("DDAK_LLM_BACKEND", "cli")
    monkeypatch.setattr(app, "load_tools", lambda: pytest.fail("must stop before initialization"))
    with pytest.raises(DdakToolError):
        app.create()


def test_main_passes_the_actual_binding_to_factory(monkeypatch):
    import uvicorn

    sentinel = object()
    create = Mock(return_value=sentinel)
    run = Mock()
    monkeypatch.setattr(app, "create", create)
    monkeypatch.setattr(uvicorn, "run", run)
    app.main()
    assert create.call_args.kwargs["cli_host"] == run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.args == (sentinel,)
