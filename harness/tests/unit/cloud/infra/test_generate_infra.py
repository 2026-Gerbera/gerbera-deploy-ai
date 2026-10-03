from dataclasses import replace
from types import SimpleNamespace

import pytest

from ddak.cloud.infra.runtime import AwsSettings
from ddak.cloud.infra.tools.generate_infra import logic
from ddak.core import runtime
from ddak.core.ai import gateway
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput
from ddak.core.redact import MAX_LEN, redact


def context(run_id: str = "run-1") -> RunContext:
    return RunContext(
        run_id,
        adapter_mode=AdapterMode.REAL,
        project="flaskr",
        mode=RunMode.BOOTSTRAP,
        repo_url="https://github.com/example/app.git",
        cloud_domain="app.example.com",
        project_settings={
            "cloud_domain": "app.example.com",
            "dns_mode": "route53",
            "hosted_zone_id": "Z123456",
        },
    )


def draft(**files: str):
    return SimpleNamespace(
        value=logic.TerraformDraft(
            files=tuple(
                logic.TerraformFileDraft(name=name, lines=tuple(source.splitlines()))
                for name, source in files.items()
            )
        ),
        source=Source.LIVE,
    )


class FakeProvider:
    def __init__(self):
        self.requests: list[AIRequest] = []
        self.replies: list[str] = []
        self.settings: list[Settings] = []

    def queue(self, **files: str) -> None:
        self.replies.append(draft(**files).value.model_dump_json())

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        return AIResponse(text=self.replies.pop(0), source=Source.LIVE)


@pytest.fixture
def gateway_provider(monkeypatch):
    provider = FakeProvider()

    def get_provider(settings: Settings):
        provider.settings.append(settings)
        return provider

    monkeypatch.setattr(gateway, "get_provider", get_provider)
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    monkeypatch.delenv("DDAK_AI_TIMEOUT_S", raising=False)
    token = runtime.current_tool.set("generate_infra")
    try:
        # call_ai 자체를 대체하면 지원하지 않는 keyword 회귀가 숨겨진다.
        assert logic.call_ai is gateway.call_ai
        yield provider
    finally:
        runtime.current_tool.reset(token)


def test_generate_platform_writes_initial_ai_bundle(gateway_provider, tmp_path):
    source = (
        'resource "aws_secretsmanager_secret" "app_secret_key" {\n'
        '  name = "ddak/${var.project}/SECRET_KEY"\n}\n'
    )
    original_settings = Settings.from_env()
    assert original_settings.ai_timeout_s == 20
    gateway_provider.queue(**{"main.tf": source})
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), context()
    )
    assert (tmp_path / "main.tf").read_text() == source
    assert result.files["main.tf"].startswith("sha256:")
    assert result.outputs["cluster_name"] == ("aws_ecs_cluster.main.name", "string")
    assert gateway_provider.requests[0].timeout_s == 240
    assert gateway_provider.requests[0].prompt_version == logic.PROMPT_VERSION
    assert gateway_provider.settings == [replace(original_settings, ai_timeout_s=240)]
    assert Settings.from_env() == original_settings
    AwsSettings(
        project="flaskr",
        account_id="123456789012",
        state_bucket="ddak-state-123456789012-abcdefghijklmnop",
        layer="platform",
        outputs=result.outputs,
    )


def test_generate_normalizes_only_double_escaped_code_owned_buildspec(monkeypatch, tmp_path):
    source = (
        'resource "aws_codebuild_project" "main" {\n'
        "  source {\n"
        '    buildspec = "version: 0.2\\\\nphases:\\\\n  build:\\\\n    commands:\\\\n'
        '      - exit 1\\\\n"\n'
        "  }\n"
        "}\n"
    )
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    monkeypatch.setattr(logic, "call_ai", lambda **_kwargs: draft(**{"codebuild.tf": source}))
    logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), context()
    )
    written = (tmp_path / "codebuild.tf").read_text()
    assert 'buildspec = "version: 0.2\\nphases:' in written
    assert 'buildspec = "version: 0.2\\\\nphases:' not in written


def test_repair_replaces_only_returned_files_and_preserves_others(gateway_provider, tmp_path):
    root = tmp_path / "bundles"
    previous, current = root / "run-1", root / "run-1-retry-2"
    previous.mkdir(parents=True)
    current.mkdir()
    old_network = 'resource "aws_default_security_group" "main" {}\n'
    old_iam = '# IAM 역할\r\nresource "aws_iam_role" "task" {}  '
    fixed_network = 'resource "aws_vpc" "main" {}\n'
    (previous / "network.tf").write_text(old_network)
    (previous / "iam.tf").write_bytes(old_iam.encode("utf-8"))
    gateway_provider.queue(**{"network.tf": fixed_network})
    base = context()
    ctx = replace(
        base,
        project_settings={
            **base.project_settings,
            "_infra_validation_feedback": "RESOURCE_NOT_ALLOWED",
            "_infra_repair_directory": str(previous),
        },
    )
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(current), layer="platform"), ctx
    )
    assert (current / "network.tf").read_text() == fixed_network
    assert (current / "iam.tf").read_bytes() == old_iam.encode("utf-8")
    assert (previous / "iam.tf").read_bytes() == old_iam.encode("utf-8")
    assert (previous / "network.tf").read_bytes() == old_network.encode("utf-8")
    assert set(result.files) == {"network.tf", "iam.tf"}
    request = gateway_provider.requests[0]
    # 공용 마스킹은 유지하되, 정제된 지시문 전체가 잘림 없이 전달되어야 한다.
    instruction = request.user.split(f"\n\n{gateway.DATA_OPEN}\n", 1)[0]
    assert instruction == redact(logic._REPAIR_INSTRUCTION, max_len=None)
    assert request.user.startswith(logic._REPAIR_PROMPT)
    assert redact(logic._PROMPT, max_len=None) in instruction
    assert instruction.endswith(logic._PROMPT.rsplit("\n\n", 1)[-1])
    assert request.timeout_s == 240
    assert request.prompt_version == f"{logic.PROMPT_VERSION}-repair"
    assert "validation_error=RESOURCE_NOT_ALLOWED" in request.user
    assert old_network in request.user
    assert old_iam in request.user
    assert "[truncated" not in request.user
    assert Settings.from_env().ai_timeout_s == 20


def test_repair_accepts_bounded_validation_history(monkeypatch, tmp_path):
    root = tmp_path / "bundles"
    previous, current = root / "run-1-retry-2", root / "run-1-retry-3"
    previous.mkdir(parents=True)
    current.mkdir()
    (previous / "network.tf").write_text('resource "aws_vpc" "main" {}\n')
    (previous / "security.tf").write_text('resource "aws_security_group" "alb" {}\n')
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    captured = {}

    def call(**kwargs):
        captured.update(kwargs)
        return draft(**{"security.tf": 'resource "aws_security_group" "alb" {}\n'})

    monkeypatch.setattr(logic, "call_ai", call)
    base = context()
    ctx = replace(
        base,
        project_settings={
            **base.project_settings,
            "_infra_validation_feedback": "RESOURCE_NOT_ALLOWED,CHECKOV_FAILED",
            "_infra_repair_directory": str(previous),
        },
    )
    logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(current), layer="platform"), ctx
    )
    assert "validation_error=RESOURCE_NOT_ALLOWED,CHECKOV_FAILED" in captured["data"]


def test_repair_rejects_full_bundle_replacement(monkeypatch, tmp_path):
    root = tmp_path / "bundles"
    previous, current = root / "run-1", root / "run-1-retry-2"
    previous.mkdir(parents=True)
    current.mkdir()
    sources = {
        "network.tf": 'resource "aws_vpc" "main" {}\n',
        "iam.tf": 'resource "aws_iam_role" "task" {}\n',
    }
    for name, source in sources.items():
        (previous / name).write_text(source)
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    monkeypatch.setattr(logic, "call_ai", lambda **_kwargs: draft(**sources))
    base = context()
    ctx = replace(
        base,
        project_settings={
            **base.project_settings,
            "_infra_validation_feedback": "RESOURCE_NOT_ALLOWED",
            "_infra_repair_directory": str(previous),
        },
    )
    with pytest.raises(DdakToolError, match="기존 파일 일부"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(current), layer="platform"), ctx
        )
    assert not any(current.iterdir())


def test_generate_requires_route53_and_namespace(monkeypatch, tmp_path):
    monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    with pytest.raises(DdakToolError, match="DDAK_DOCKERHUB_NAMESPACE"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), context()
        )


def test_app_layer_stops_until_new_secret_contract_exists(tmp_path):
    with pytest.raises(DdakToolError, match="새 시크릿 키 목록 계약"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="app"), context()
        )


def test_tool_timeout_does_not_change_other_gateway_calls(gateway_provider, tmp_path):
    source = 'resource "aws_vpc" "main" {}\n'
    gateway_provider.queue(**{"main.tf": source})
    gateway_provider.queue(**{"main.tf": source})
    logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), context()
    )
    token = runtime.current_tool.set("generate_plan")
    try:
        gateway.call_ai(instruction="test", data="test", output_model=logic.TerraformDraft)
    finally:
        runtime.current_tool.reset(token)
    assert [request.timeout_s for request in gateway_provider.requests] == [240, 20]
    assert [request.purpose for request in gateway_provider.requests] == [
        "generate_infra",
        "generate_plan",
    ]


@pytest.mark.parametrize("mode", ["draft", "repair"])
@pytest.mark.parametrize("data_size", [MAX_LEN, MAX_LEN + 1])
def test_gateway_data_limit_preserves_or_rejects_complete_context(
    gateway_provider, monkeypatch, tmp_path, mode, data_size
):
    ctx = replace(context(), repo_url="https://github.com/example/app")
    directory = tmp_path / "bundles" / "run-1-retry-2"
    directory.mkdir(parents=True)
    base = logic._safe_data(ctx, "example")
    if mode == "draft":
        ctx = replace(ctx, repo_url=ctx.repo_url + "x" * (data_size - len(base)))
        expected_data = logic._safe_data(ctx, "example")
        gateway_provider.queue(**{"main.tf": 'resource "aws_vpc" "main" {}\n'})
    else:
        previous = directory.parent / "run-1"
        previous.mkdir()
        feedback = "RESOURCE_NOT_ALLOWED"
        sources = {
            "iam.tf": 'resource "aws_iam_role" "task" {}\n',
            "network.tf": "# \n",
        }
        padding = data_size - len(logic._repair_data(base, feedback, sources))
        sources["network.tf"] = "# " + "x" * padding + "\n"
        for name, source in sources.items():
            (previous / name).write_text(source)
        ctx = replace(
            ctx,
            project_settings={
                **ctx.project_settings,
                "_infra_validation_feedback": feedback,
                "_infra_repair_directory": str(previous),
            },
        )
        expected_data = logic._repair_data(base, feedback, sources)
        gateway_provider.queue(**{"network.tf": 'resource "aws_vpc" "main" {}\n'})
    assert len(expected_data) == data_size
    inp = GenerateInfraInput(run_id="run-1", directory=str(directory), layer="platform")
    if data_size > MAX_LEN:

        def unexpected_call(**_kwargs):
            pytest.fail("4096자 초과 data는 call_ai 호출 전에 거부해야 한다")

        monkeypatch.setattr(logic, "call_ai", unexpected_call)
        with pytest.raises(DdakToolError, match=r"NEEDS_CONTEXT.*4096") as error:
            logic.generate_infra(inp, ctx)
        assert error.value.code is ErrorCode.CONFIG_INVALID
        assert error.value.needs_human
        assert not gateway_provider.requests
        assert not gateway_provider.settings
        assert not any(directory.iterdir())
    else:
        logic.generate_infra(inp, ctx)
        request = gateway_provider.requests[0]
        assert f"{gateway.DATA_OPEN}\n{expected_data}\n{gateway.DATA_CLOSE}" in request.user
        assert "[truncated" not in request.user
        assert request.timeout_s == 240


def test_data_limit_checks_length_after_redaction(gateway_provider, monkeypatch, tmp_path):
    ctx = replace(context(), repo_url="https://github.com/example/app?api_key=x")
    padding = MAX_LEN - len(logic._safe_data(ctx, "example"))
    ctx = replace(ctx, repo_url=ctx.repo_url.replace("?api_key=x", "x" * padding + "?api_key=x"))
    assert len(logic._safe_data(ctx, "example")) == MAX_LEN

    def unexpected_call(**_kwargs):
        pytest.fail("정제 후 4096자 초과 data는 call_ai 호출 전에 거부해야 한다")

    monkeypatch.setattr(logic, "call_ai", unexpected_call)
    with pytest.raises(DdakToolError, match=r"NEEDS_CONTEXT.*4096") as error:
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), ctx
        )
    assert error.value.code is ErrorCode.CONFIG_INVALID
    assert error.value.needs_human
    assert not gateway_provider.requests
    assert not gateway_provider.settings
    assert not any(tmp_path.iterdir())


def test_generate_passes_redacted_data_to_gateway(gateway_provider, monkeypatch, tmp_path):
    ctx = replace(context(), repo_url="https://github.com/example/app?api_key=" + "x" * MAX_LEN)
    raw_data = logic._safe_data(ctx, "example")
    safe_data = redact(raw_data)
    assert len(raw_data) > MAX_LEN
    assert len(safe_data) < MAX_LEN
    captured = {}

    def call(**kwargs):
        captured.update(kwargs)
        return gateway.call_ai(**kwargs)

    monkeypatch.setattr(logic, "call_ai", call)
    gateway_provider.queue(**{"main.tf": 'resource "aws_vpc" "main" {}\n'})
    logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), ctx
    )
    assert captured["data"] == safe_data
    assert f"{gateway.DATA_OPEN}\n{safe_data}\n{gateway.DATA_CLOSE}" in (
        gateway_provider.requests[0].user
    )


def test_generate_rejects_symlink_directory_before_resolve(gateway_provider, tmp_path):
    target = tmp_path / "run-1"
    target.mkdir()
    link = tmp_path / "run-1-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(DdakToolError, match="비어 있는 인프라 번들 디렉터리"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(link), layer="platform"), context()
        )
    assert not gateway_provider.requests
    assert not any(target.iterdir())


def test_repair_rejects_symlink_directory_before_resolve(gateway_provider, tmp_path):
    previous = tmp_path / "run-1"
    previous.mkdir()
    (previous / "network.tf").write_text('resource "aws_vpc" "main" {}\n')
    (previous / "iam.tf").write_text('resource "aws_iam_role" "task" {}\n')
    link = tmp_path / "run-1-link"
    link.symlink_to(previous, target_is_directory=True)
    current = tmp_path / "run-1-retry-2"
    current.mkdir()
    ctx = context()
    ctx = replace(
        ctx,
        project_settings={
            **ctx.project_settings,
            "_infra_validation_feedback": "RESOURCE_NOT_ALLOWED",
            "_infra_repair_directory": str(link),
        },
    )
    with pytest.raises(DdakToolError, match="인프라 교정 원본 경로 오류"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(current), layer="platform"), ctx
        )
    assert not gateway_provider.requests
    assert not any(current.iterdir())
