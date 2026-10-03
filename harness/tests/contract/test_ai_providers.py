"""call_ai backend(cli/api/replay) 규칙. 실제 CLI·API는 부르지 않는다(실호출은 llm 마커, 수동)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.core.ai.providers import AIRequest, get_provider
from ddak.core.ai.providers.api import AnthropicApiProvider
from ddak.core.ai.providers.cli import (
    FORBIDDEN_FLAGS,
    ClaudeCliProvider,
    build_argv,
    build_env,
    parse_result,
)
from ddak.core.ai.providers.replay import ReplayProvider
from ddak.core.ai.status import llm_status
from ddak.core.config import Settings
from ddak.core.contracts.enums import LLMBackend, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode

REQ = AIRequest(purpose="generate_plan", system="sys", user="u", json_schema={"type": "object"})


def test_cli_argv_turns_every_tool_off() -> None:
    argv = build_argv("claude", system="sys", json_schema={"type": "object"}, model=None)
    assert argv[:2] == ["claude", "-p"]
    for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert not FORBIDDEN_FLAGS.intersection(argv)
    assert "u" not in argv  # 프롬프트는 stdin으로만


def test_cli_env_allowlist_keeps_user_and_drops_keys() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "USER": "x",
        "LANG": "ko_KR.UTF-8",
        "ANTHROPIC_API_KEY": "sk-ant-fake",
        "DDAK_LLM_API_KEY": "sk-ant-fake",
        "AWS_PROFILE": "ddak-dev",
        "AWS_SECRET_ACCESS_KEY": "fake",
        "GITHUB_TOKEN": "fake",
    }
    env = build_env(parent)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x", "USER": "x", "LANG": "ko_KR.UTF-8"}


def test_cli_result_parsing() -> None:
    stdout = json.dumps(
        {
            "structured_output": {"tiers": ["was"]},
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_cost_usd": 0.004,
            "duration_ms": 2993,
        }
    )
    response = parse_result(stdout, model="claude-sonnet-5-5")
    assert json.loads(response.text) == {"tiers": ["was"]}
    assert response.source is Source.LIVE
    assert response.usage is not None and response.usage.output_tokens == 5
    with pytest.raises(DdakToolError) as info:
        parse_result(json.dumps({"is_error": True}), model=None)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_cli_missing_binary_is_unavailable(tmp_path: Path) -> None:
    provider = ClaudeCliProvider(str(tmp_path / "no-such-claude"))
    with pytest.raises(DdakToolError) as info:
        provider.complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_api_without_key_is_unavailable() -> None:
    with pytest.raises(DdakToolError) as info:
        AnthropicApiProvider(None).complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE


def test_replay_hit_and_miss(tmp_path: Path) -> None:
    provider = ReplayProvider(tmp_path)
    with pytest.raises(DdakToolError) as info:
        provider.complete(REQ)
    assert info.value.code is ErrorCode.AI_UNAVAILABLE
    provider.save(REQ, {"tiers": ["was"]})
    response = provider.complete(REQ)
    assert response.source is Source.REPLAY
    assert json.loads(response.text) == {"tiers": ["was"]}
    changed = AIRequest(purpose="generate_plan", system="sys", user="u2", json_schema={})
    with pytest.raises(DdakToolError):
        provider.complete(changed)  # 입력이 바뀌면 키가 바뀐다


@pytest.mark.parametrize(
    ("backend", "name"),
    [(LLMBackend.CLI, "cli"), (LLMBackend.API, "groq"), (LLMBackend.REPLAY, "replay")],
)
def test_backend_is_chosen_by_settings(backend: LLMBackend, name: str) -> None:
    assert get_provider(Settings(llm_backend=backend)).name == name


def test_default_backend_is_replay_and_keys_are_hidden() -> None:
    settings = Settings.from_env({"DDAK_LLM_API_KEY": "sk-ant-fake-value"})
    assert settings.llm_backend is LLMBackend.REPLAY
    assert "sk-ant-fake-value" not in repr(settings)


def test_llm_status_never_shows_key_value() -> None:
    status = llm_status(Settings(llm_backend=LLMBackend.API, llm_api_key="sk-ant-fake-value"))
    assert status["backend"] == "api"
    assert "sk-ant-fake-value" not in json.dumps(status, ensure_ascii=False)
    assert llm_status(Settings())["backend"] == "replay"
