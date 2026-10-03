"""수정11 registry/HTTP/status 회귀. 소켓·실제 runner·kill을 테스트에서 차단한다."""

from __future__ import annotations

import io
import json
import subprocess
import traceback
import urllib.error
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from ddak.core import config, runtime
from ddak.core.ai.gateway import ask_jev, call_ai
from ddak.core.ai.providers import (
    AIRequest,
    AIResponse,
    ProviderSpec,
    get_jev_client,
    get_provider,
    provider_catalog,
    provider_key,
    provider_model,
    provider_status,
    register_provider,
    registry,
    validate_provider_selection,
)
from ddak.core.ai.providers import (
    test_provider_connection as probe,
)
from ddak.core.ai.providers.anthropic_api import ANTHROPIC_ENDPOINT, ClaudeApiProvider
from ddak.core.ai.providers.api import AnthropicApiProvider
from ddak.core.ai.providers.cli import cli_status
from ddak.core.ai.providers.groq_api import GROQ_ENDPOINT, GroqApiProvider
from ddak.core.ai.providers.jev import JevAnswer, JevQuestion
from ddak.core.config import Settings, require_local_cli
from ddak.core.contracts.enums import LLMBackend, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import REDACTED
from ddak.core.runtime import tool_context

KEY = "sk-ant-" + "fake-test-" + "abcdefgh123456789"
GROQ_KEY = "gsk_" + "fake-test-abcdefgh123456789"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", registry._REGISTRY.copy())
    monkeypatch.setattr(registry, "_VERIFIED", {})
    kinds = config._PROVIDER_KINDS.copy()
    monkeypatch.setattr(config, "_PROVIDER_KINDS", kinds)
    monkeypatch.setattr(registry, "_PROVIDER_KINDS", kinds)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("실제 네트워크/runner/kill 호출 금지")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("os.killpg", forbidden)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


class FakeProvider:
    name = "custom-api"

    def __init__(self, *, source: Source = Source.LIVE) -> None:
        self.source = source
        self.seen: list[AIRequest] = []
        self.failure: Exception | None = None
        self.invalid = False

    def complete(self, req: AIRequest) -> AIResponse:
        self.seen.append(req)
        if self.failure:
            raise self.failure
        if self.invalid:
            return AIResponse(text='{"unexpected":1}', source=self.source)
        text = (
            '{"answers":[{"id":"q","probability":1}]}'
            if "answers" in req.json_schema.get("properties", {})
            else '{"ok":true}'
        )
        return AIResponse(text=text, source=self.source)


def test_one_registration_drives_generation_judgment_status_and_probe() -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(
            id="custom-api",
            label="Custom",
            kind="api",
            default_model="fake-model",
            generation_factory=lambda cfg: fake,
        )
    )
    cfg = Settings(llm_provider="custom-api", judgment_provider="custom-api")
    assert get_provider(cfg) is fake
    assert provider_status("custom-api", cfg)["status"] == "gray"
    assert len(fake.seen) == 0
    with tool_context("generate_plan", "r"):
        result = call_ai(instruction="classify", data="data", output_model=Output, settings=cfg)
        answers = ask_jev(
            state="data",
            questions=[JevQuestion(id="q", kind="noul", text="question")],
            settings=cfg,
        )
    assert result.value.ok and answers[0].probability == 1
    assert get_jev_client(cfg).name == "custom-api"
    assert fake.seen[0].model == "fake-model"
    assert probe("custom-api", cfg)["status"] == "green"
    status = provider_status("custom-api", cfg)
    assert status["status"] == "green" and status["verified_at"]
    assert provider_status("custom-api", replace(cfg, llm_model="changed"))["status"] == "gray"
    assert (
        provider_status("custom-api", replace(cfg, judgment_model="changed"), role="judgment")[
            "status"
        ]
        == "gray"
    )
    assert (
        provider_status("custom-api", replace(cfg, judgment_model="changed"))["status"] == "green"
    )


def test_catalog_has_only_public_metadata_and_key_presence() -> None:
    cfg = Settings(anthropic_api_key=KEY, groq_api_key=GROQ_KEY)
    catalog = {item["id"]: item for item in provider_catalog(cfg)}
    assert set(catalog) == {"claude-cli", "claude-api", "groq", "replay", "jev"}
    assert catalog["claude-api"]["key_name"] == "anthropic_api_key"
    assert catalog["groq"]["key_name"] == "groq_api_key"
    assert catalog["claude-api"]["key_configured"] is True
    assert catalog["groq"]["kind"] == "api"
    assert catalog["claude-cli"]["default_model"] == "claude-sonnet-5-5"
    assert catalog["jev"]["roles"] == ["judgment"] and "미연결" in catalog["jev"]["label"]
    assert KEY not in json.dumps(catalog) and KEY not in repr(cfg)
    assert GROQ_KEY not in json.dumps(catalog)
    assert set(provider_catalog()[0]) == {
        "id",
        "label",
        "kind",
        "roles",
        "auth",
        "default_model",
        "models",
        "key_name",
    }
    assert cfg.llm_effort == "low"


def test_settings_env_and_legacy_api_migration() -> None:
    cfg = Settings.from_env(
        {
            "DDAK_LLM_PROVIDER": "claude-api",
            "DDAK_JUDGMENT_PROVIDER": "groq",
            "DDAK_JUDGMENT_MODEL": "judgment",
            "DDAK_ANTHROPIC_API_KEY": KEY,
            "DDAK_GROQ_API_KEY": GROQ_KEY,
        }
    )
    assert cfg.llm_provider == "claude-api" and cfg.judgment_provider == "groq"
    assert cfg.judgment_model == "judgment" and cfg.anthropic_api_key == KEY
    assert cfg.groq_api_key == GROQ_KEY
    assert isinstance(get_provider(cfg), ClaudeApiProvider)
    assert AnthropicApiProvider is GroqApiProvider
    legacy = Settings(llm_backend=LLMBackend.API, llm_api_key=GROQ_KEY, llm_model="legacy")
    assert isinstance(get_provider(legacy), GroqApiProvider)
    assert get_provider(legacy).name == "groq"
    assert get_provider(Settings()).name == "replay"
    assert get_provider(Settings(llm_backend=LLMBackend.CLI)).name == "cli"
    assert provider_status("groq", legacy)["status"] == "gray"


@pytest.mark.parametrize(
    "id,role", [("missing", "generation"), ("jev", "generation"), ("groq", "invalid")]
)
def test_invalid_selection_fails_closed(id: str, role: Any) -> None:
    with pytest.raises(DdakToolError) as info:
        validate_provider_selection(id, role)
    assert info.value.code is ErrorCode.CONFIG_INVALID


def test_explicit_models_do_not_cross_providers() -> None:
    cfg = Settings(
        llm_provider="groq",
        llm_model="llama",
        judgment_provider="claude-api",
        judgment_model="judge-model",
    )
    assert provider_model(cfg) == "llama"
    assert provider_model(cfg, "judgment") == "judge-model"
    assert provider_model(replace(cfg, judgment_model=None), "judgment") == "claude-sonnet-5-5"
    cfg = replace(
        cfg, judgment_provider="groq", judgment_model="judge-groq", groq_model="legacy-judge"
    )
    assert get_jev_client(cfg).model == "judge-groq"


def test_generic_cli_selected_provider_reuses_local_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(
            id="custom-cli", label="Custom CLI", kind="cli", generation_factory=lambda cfg: fake
        )
    )
    monkeypatch.setattr(Path, "exists", lambda self: False)
    for cfg in (Settings(llm_provider="custom-cli"), Settings(judgment_provider="custom-cli")):
        require_local_cli(cfg, host="127.0.0.1", environ={})
        for host, env in (
            ("192.0.2.1", {}),
            ("127.0.0.1", {"SSH_CONNECTION": "fake"}),
            ("127.0.0.1", {"CI": "true"}),
        ):
            with pytest.raises(DdakToolError):
                require_local_cli(cfg, host=host, environ=env)
    require_local_cli(
        Settings(llm_backend=LLMBackend.CLI, llm_provider="replay"),
        host=None,
        environ={"CI": "true"},
    )


@pytest.mark.parametrize(
    "body,code,expected",
    [
        ({"loggedIn": True, "email": "private@example.invalid", "unexpected": KEY}, 0, "gray"),
        ({"loggedIn": False}, 0, "red"),
        ({"loggedIn": True}, 1, "red"),
        ({"loggedIn": "true"}, 0, "red"),
        ([], 0, "red"),
        ("Claude 5.5", 0, "red"),
    ],
)
def test_cli_status_requires_auth_json_and_never_leaks(
    body: Any, code: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, code, stdout=json.dumps(body) if not isinstance(body, str) else body, stderr=KEY
        )

    monkeypatch.setattr("subprocess.run", runner)
    status = cli_status("fake-cli")
    assert seen[0][0] == ["fake-cli", "auth", "status", "--json"]
    assert status["status"] == expected and status["ok"] is False
    assert KEY not in json.dumps(status) and "private@" not in json.dumps(status)
    assert set(seen[0][1]["env"]) <= {"PATH", "HOME", "USER", "LANG"}
    assert seen[0][1]["cwd"]


@pytest.mark.parametrize(
    "error", [FileNotFoundError("private@example.invalid"), subprocess.TimeoutExpired("fake", 1)]
)
def test_cli_status_runner_errors_are_red(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runner(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr("subprocess.run", runner)
    status = cli_status()
    assert status["status"] == "red" and "private@" not in json.dumps(status)


def test_key_present_and_hook_green_do_not_prove_readiness() -> None:
    cfg = Settings(anthropic_api_key=KEY, groq_api_key=GROQ_KEY)
    assert provider_status("claude-api", cfg)["status"] == "gray"
    assert provider_status("groq", cfg)["status"] == "gray"
    assert provider_status("claude-api", Settings())["status"] == "red"
    for id in ("replay", "jev"):
        assert probe(id, cfg)["status"] == "gray"
    register_provider(
        ProviderSpec(
            id="fake-ready",
            label="Fake",
            kind="api",
            generation_factory=lambda cfg: FakeProvider(),
            status=lambda cfg: {"status": "green", "detail": "test@example.invalid"},
        )
    )
    status = provider_status("fake-ready", cfg)
    assert status["status"] == "gray" and "test@" not in status["detail"]


@pytest.mark.parametrize("source", [Source.REPLAY, Source.CACHE, Source.FIXTURE])
def test_probe_never_greens_nonlive(source: Source) -> None:
    fake = FakeProvider(source=source)
    register_provider(
        ProviderSpec(id="fake", label="Fake", kind="api", generation_factory=lambda cfg: fake)
    )
    cfg = Settings(llm_provider="fake")
    assert probe("fake", cfg)["status"] == "gray"
    assert provider_status("fake", cfg)["status"] == "gray"


def test_fixed_probe_reuses_schema_and_does_not_set_tool_context() -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(id="fake", label="Fake", kind="api", generation_factory=lambda cfg: fake)
    )
    cfg = Settings(llm_provider="fake", ai_retries=0)
    assert runtime.current_tool.get() is None
    assert probe("fake", cfg)["status"] == "green"
    assert runtime.current_tool.get() is None
    req = fake.seen[0]
    assert req.purpose == "provider_connection_test" and req.prompt_version == "connection-v1"
    assert req.user == 'Return exactly {"ok":true}.\n\n<untrusted_data>\n\n</untrusted_data>'
    assert req.json_schema["additionalProperties"] is False
    fake.invalid = True
    assert probe("fake", cfg)["status"] == "red"
    assert provider_status("fake", cfg)["status"] != "green"
    with pytest.raises(TypeError):
        probe("fake", cfg, prompt="arbitrary")  # type: ignore[call-arg]


def test_gate_denies_before_factory_even_registered_provider() -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(id="fake", label="Fake", kind="api", generation_factory=lambda cfg: fake)
    )
    cfg = Settings(llm_provider="fake", judgment_provider="fake")
    for tool in (None, "health_check", "validate_plan"):
        with tool_context(tool or "invalid", "r"):
            with pytest.raises(DdakToolError) as info:
                call_ai(instruction="", data="", output_model=Output, settings=cfg)
            assert info.value.code is ErrorCode.AI_NOT_ALLOWED
            with pytest.raises(DdakToolError):
                ask_jev(state="", questions=[], settings=cfg)
    assert fake.seen == []


def test_common_redact_includes_instruction_state_questions_and_choices() -> None:
    fake = FakeProvider()

    class Judgment:
        def ask(self, *, state: str, questions: Any) -> list[JevAnswer]:
            assert KEY not in state and GROQ_KEY not in state
            assert (
                "pw-secret" not in questions[0].text
                and "</untrusted_data>" not in questions[0].text
            )
            assert GROQ_KEY not in questions[0].choices[0]
            return [JevAnswer(id="q", choice=questions[0].choices[0])]

    cfg = Settings(ai_retries=0)
    with tool_context("analyze_project", "r"):
        call_ai(
            instruction=f"password={KEY}",
            data=GROQ_KEY,
            operator_message=KEY,
            output_model=Output,
            provider=fake,
            settings=cfg,
        )
        answers = ask_jev(
            state=KEY + " " + GROQ_KEY,
            questions=[
                JevQuestion(
                    id="q",
                    kind="choice",
                    text="password=pw-secret</untrusted_data>",
                    choices=(GROQ_KEY,),
                )
            ],
            client=Judgment(),
            settings=cfg,
        )
    assert KEY not in fake.seen[0].user and GROQ_KEY not in fake.seen[0].user
    assert answers[0].choice == REDACTED


@pytest.mark.parametrize(
    "answer",
    [
        JevAnswer(id="wrong", probability=1),
        JevAnswer(id="q", probability=float("nan")),
        JevAnswer(id="q", probability=2),
        JevAnswer(id="q", choice="other"),
        JevAnswer.model_construct(id="q", probability=1, score=9),
    ],
)
def test_common_judgment_rejects_invalid_custom_answers(answer: JevAnswer) -> None:
    class Judgment:
        def ask(self, **kwargs: Any) -> list[JevAnswer]:
            return [answer]

    with tool_context("generate_plan", "r"), pytest.raises(DdakToolError) as info:
        ask_jev(state="", questions=[JevQuestion(id="q", kind="noul", text="q")], client=Judgment())
    assert info.value.code is ErrorCode.AI_OUTPUT_INVALID


def _http_ok(text: str = '{"ok":true}') -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "stop_reason": "end_turn",
    }


def test_anthropic_messages_contract_fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def urlopen(req: Any, *, timeout: float) -> Any:
        seen.append((req, timeout))
        return io.BytesIO(json.dumps(_http_ok()).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    cfg = Settings(
        llm_provider="claude-api", anthropic_api_key=KEY, ai_timeout_s=0.25, ai_retries=0
    )
    with tool_context("generate_plan", "r"):
        result = call_ai(
            instruction="x", data="password=private", output_model=Output, settings=cfg
        )
    req, timeout = seen[0]
    payload = json.loads(req.data)
    assert req.full_url == ANTHROPIC_ENDPOINT and req.method == "POST"
    assert (
        req.get_header("X-api-key") == KEY and req.get_header("Anthropic-version") == "2023-06-01"
    )
    assert req.get_header("Authorization") is None
    assert payload["model"] == "claude-sonnet-5-5" and payload["output_config"] == {"effort": "low"}
    assert "tools" not in payload and "JSON Schema" in payload["system"]
    assert "private" not in payload["messages"][0]["content"] and timeout == 0.25
    assert result.source is Source.LIVE and result.usage.input_tokens == 2
    assert KEY not in repr(get_provider(cfg))


@pytest.mark.parametrize(
    "failure", [401, 403, 429, 500, 503, "timeout", "url", "os", "malformed", "tool"]
)
def test_anthropic_errors_retries_and_no_exception_secrets(
    failure: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def urlopen(req: Any, *, timeout: float) -> Any:
        calls.append(req)
        if isinstance(failure, int):
            raise urllib.error.HTTPError(req.full_url, failure, KEY, {}, None)
        if failure == "timeout":
            raise TimeoutError(KEY)
        if failure == "url":
            raise urllib.error.URLError(KEY)
        if failure == "os":
            raise OSError(KEY)
        if failure == "tool":
            return io.BytesIO(
                json.dumps({"content": [{"type": "tool_use", "input": KEY}]}).encode()
            )
        return io.BytesIO(KEY.encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    cfg = Settings(llm_provider="claude-api", anthropic_api_key=KEY, ai_retries=1)
    with tool_context("generate_plan", "r"), pytest.raises(DdakToolError) as info:
        call_ai(instruction="x", data="", output_model=Output, settings=cfg)
    assert len(calls) == 2 and info.value.code is ErrorCode.AI_UNAVAILABLE
    assert KEY not in "".join(traceback.format_exception(info.value))
    assert info.value.__cause__ is None
    assert probe("claude-api", cfg)["status"] == "red"


def test_groq_generation_and_judgment_probe_keep_groq_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def urlopen(req: Any, *, timeout: float) -> Any:
        seen.append(req)
        payload = json.loads(req.data)
        answer = (
            '{"answers":[{"id":"connection","probability":1}]}'
            if payload["max_tokens"] == 2048
            else '{"ok":true}'
        )
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": answer}}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    cfg = Settings(
        llm_provider="groq",
        judgment_provider="groq",
        llm_model="generation",
        judgment_model="judgment",
        groq_api_key=GROQ_KEY,
        llm_api_key=KEY,
    )
    assert probe("groq", cfg, role="generation")["status"] == "green"
    assert probe("groq", cfg, role="judgment")["status"] == "green"
    assert [json.loads(r.data)["model"] for r in seen] == ["generation", "judgment"]
    assert all(
        r.full_url == GROQ_ENDPOINT and r.get_header("Authorization") == f"Bearer {GROQ_KEY}"
        for r in seen
    )
    assert get_provider(cfg).name == "groq"


def test_generic_provider_key_injected_saved_selected_and_redacted() -> None:
    fake = FakeProvider()
    opaque = "opaque-" + "test-credential"
    key_store = {"custom_token": opaque}
    register_provider(
        ProviderSpec(
            id="new-provider",
            label="New",
            kind="api",
            auth="api-key",
            key_name="custom_token",
            generation_factory=lambda cfg: fake,
            status=lambda cfg: {
                "status": "gray",
                "detail": provider_key(cfg, "custom_token") or "no key",
            },
        )
    )
    cfg = Settings(
        llm_provider="new-provider", judgment_provider="new-provider", provider_keys=key_store
    )
    key_store["custom_token"] = "mutated"
    assert provider_key(cfg, "custom_token") == opaque
    assert opaque not in repr(cfg)
    assert opaque not in json.dumps(provider_catalog(cfg))
    assert (
        next(item for item in provider_catalog(cfg) if item["id"] == "new-provider")[
            "key_configured"
        ]
        is True
    )
    assert opaque not in provider_status("new-provider", cfg)["detail"]
    with tool_context("generate_plan", "r"):
        call_ai(
            instruction=opaque,
            data=opaque,
            operator_message=opaque,
            output_model=Output,
            settings=cfg,
        )
        ask_jev(
            state=opaque, questions=[JevQuestion(id="q", kind="noul", text=opaque)], settings=cfg
        )
    assert all(opaque not in req.user for req in fake.seen)
    assert probe("new-provider", cfg)["status"] == "green"
    assert (
        provider_status("new-provider", replace(cfg, provider_keys={"custom_token": "different"}))[
            "status"
        ]
        == "gray"
    )
    with pytest.raises(TypeError):
        cfg.provider_keys["custom_token"] = "new"  # type: ignore[index]


def test_generic_keys_override_builtins_and_explicit_empty_disables_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def urlopen(req: Any, *, timeout: float) -> Any:
        seen.append(req)
        return io.BytesIO(json.dumps(_http_ok()).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    cfg = Settings(
        llm_provider="claude-api",
        anthropic_api_key="fallback",
        provider_keys={"anthropic_api_key": KEY},
    )
    assert provider_key(cfg, "anthropic_api_key") == KEY
    assert probe("claude-api", cfg)["status"] == "green"
    assert seen[0].get_header("X-api-key") == KEY
    assert (
        provider_status("claude-api", replace(cfg, provider_keys={"anthropic_api_key": ""}))[
            "status"
        ]
        == "red"
    )
    assert provider_key(replace(cfg, provider_keys={}), "anthropic_api_key") == "fallback"
    cfg = Settings(
        llm_provider="groq",
        provider_keys={"groq_api_key": GROQ_KEY},
        groq_api_key="fallback",
        llm_model="model",
    )
    assert get_provider(cfg)._key == GROQ_KEY
    assert get_jev_client(cfg)._key == GROQ_KEY
    assert provider_status("groq", cfg)["status"] == "gray"


def test_env_groq_generation_retains_key_with_claude_judgment() -> None:
    cfg = Settings.from_env(
        {
            "DDAK_LLM_PROVIDER": "groq",
            "DDAK_JUDGMENT_PROVIDER": "claude-cli",
            "DDAK_GROQ_API_KEY": GROQ_KEY,
        }
    )
    assert cfg.groq_api_key == GROQ_KEY
    assert get_provider(cfg)._key == GROQ_KEY


def test_failed_probe_persists_red_until_next_success() -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(id="fake", label="Fake", kind="api", generation_factory=lambda cfg: fake)
    )
    cfg = Settings(llm_provider="fake", ai_retries=0)
    assert probe("fake", cfg)["status"] == "green"
    fake.invalid = True
    assert probe("fake", cfg)["status"] == "red"
    assert provider_status("fake", cfg)["status"] == "red"
    fake.invalid = False
    assert probe("fake", cfg)["status"] == "green"


def test_main_effective_settings_consumes_arbitrary_fake_vault_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddak.app import _effective_project_settings

    class FakeVault:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}

        def put(self, project: str, key: str, value: str) -> None:
            self.values[project, key] = value

        def get(self, project: str, key: str) -> str | None:
            return self.values.get((project, key))

    fake = FakeProvider()
    register_provider(
        ProviderSpec(
            id="external-fake",
            label="External Fake",
            kind="api",
            key_name="external_key",
            default_model="external-v1",
            generation_factory=lambda cfg: fake,
        )
    )
    for key in ("DDAK_LLM_BACKEND", "DDAK_LLM_MODEL", "DDAK_JEV_BACKEND", "DDAK_JUDGMENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    vault = FakeVault()
    opaque = "external-" + "saved-fake-credential"
    vault.put("demo", "external_key", opaque)
    saved = {"generation_provider": "external-fake", "judgment_provider": "external-fake"}
    cfg = _effective_project_settings(Settings(), "demo", saved, vault, cli_host="127.0.0.1")
    assert cfg.llm_provider == "external-fake" and cfg.judgment_provider == "external-fake"
    assert cfg.llm_model == "external-v1" and cfg.judgment_model == "external-v1"
    assert cfg.provider_keys["external_key"] == opaque
    assert provider_status("external-fake", cfg)["status"] == "gray"
    with tool_context("generate_plan", "r"):
        assert call_ai(instruction="x", data=opaque, output_model=Output, settings=cfg).value.ok
        assert (
            ask_jev(
                state=opaque, questions=[JevQuestion(id="q", kind="noul", text="q")], settings=cfg
            )[0].probability
            == 1
        )
    assert all(opaque not in req.user for req in fake.seen)
    assert probe("external-fake", cfg, role="generation")["status"] == "green"
    assert probe("external-fake", cfg, role="judgment")["status"] == "green"


def test_anthropic_and_groq_status_keys_are_independent() -> None:
    claude = Settings(anthropic_api_key=KEY)
    groq = Settings(groq_api_key=GROQ_KEY)
    assert provider_status("claude-api", claude)["status"] == "gray"
    assert provider_status("groq", claude)["status"] == "red"
    assert provider_status("claude-api", groq)["status"] == "red"
    assert provider_status("groq", groq)["status"] == "gray"
    generic = Settings(provider_keys={"groq_api_key": GROQ_KEY})
    assert provider_status("groq", generic)["status"] == "gray"
    assert provider_status("claude-api", generic)["status"] == "red"


@pytest.mark.parametrize(
    "payload", ['{"ok":"yes"}', '{"ok":1}', '{"ok":null}', '{"ok":true,"extra":1}']
)
def test_common_generation_enforces_json_schema_without_type_coercion(payload: str) -> None:
    class Invalid(FakeProvider):
        def complete(self, req: AIRequest) -> AIResponse:
            return AIResponse(text=payload, source=Source.LIVE)

    with tool_context("generate_plan", "r"), pytest.raises(DdakToolError) as info:
        call_ai(instruction="x", data="", output_model=Output, provider=Invalid())
    assert info.value.code is ErrorCode.AI_OUTPUT_INVALID


@pytest.mark.parametrize(
    "payload", ['{"ok":false}', '{"ok":1}', '{"ok":"true"}', '{"ok":true,"extra":1}']
)
def test_fixed_probe_requires_boolean_true_only(payload: str) -> None:
    class Invalid(FakeProvider):
        def complete(self, req: AIRequest) -> AIResponse:
            return AIResponse(text=payload, source=Source.LIVE)

    register_provider(
        ProviderSpec(
            id="invalid", label="Invalid", kind="api", generation_factory=lambda cfg: Invalid()
        )
    )
    assert probe("invalid", Settings())["status"] == "red"


def test_generic_auth_key_uppercase_and_missing_default_status() -> None:
    register_provider(
        ProviderSpec(
            id="custom",
            label="Custom",
            kind="api",
            key_name="CUSTOM_API_KEY",
            generation_factory=lambda cfg: FakeProvider(),
        )
    )
    assert provider_status("custom", Settings())["status"] == "red"
    cfg = Settings(provider_keys={"CUSTOM_API_KEY": "fake-" + "opaque-value"})
    assert provider_status("custom", cfg)["status"] == "gray"
    assert provider_key(cfg, "CUSTOM_API_KEY") == cfg.provider_keys["CUSTOM_API_KEY"]


def test_generic_judgment_does_not_assume_anthropic_model() -> None:
    fake = FakeProvider()
    register_provider(
        ProviderSpec(
            id="model-less", label="Model-less", kind="api", generation_factory=lambda cfg: fake
        )
    )
    cfg = Settings(judgment_provider="model-less")
    client = get_jev_client(cfg)
    assert client.model is None
    with tool_context("generate_plan", "r"):
        assert (
            ask_jev(state="", questions=[JevQuestion(id="q", kind="noul", text="q")], settings=cfg)[
                0
            ].probability
            == 1
        )
    assert fake.seen[0].model is None


def test_reserved_jev_presence_is_metadata_only() -> None:
    cfg = Settings(jev_key_configured=True)
    assert (
        next(item for item in provider_catalog(cfg) if item["id"] == "jev")["key_configured"]
        is True
    )
    assert provider_status("jev", cfg)["status"] == "gray"
    assert probe("jev", cfg)["status"] == "gray"
    assert isinstance(hash(cfg), int)
