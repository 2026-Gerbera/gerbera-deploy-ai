from dataclasses import replace
from types import SimpleNamespace

import pytest

from ddak.cloud.infra.runtime import AwsSettings
from ddak.cloud.infra.tools.generate_infra import logic
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput


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


def test_platform_prompt_requires_runtime_secret_tls_and_minimal_egress() -> None:
    assert "aws_secretsmanager_secret.app_database_url" in logic._PROMPT
    assert "ELBSecurityPolicy-TLS13-1-2-2021-06" in logic._PROMPT
    assert "least-privilege egress" in logic._PROMPT
    assert "rolling deployment percentages 100/200" in logic._PROMPT


def test_generate_platform_writes_initial_ai_bundle(monkeypatch, tmp_path):
    source = (
        'resource "aws_secretsmanager_secret" "app_secret_key" {\n'
        '  name = "ddak/${var.project}/SECRET_KEY"\n}\n'
    )
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    monkeypatch.setattr(logic, "call_ai", lambda **_kwargs: draft(**{"main.tf": source}))
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"), context()
    )
    assert (tmp_path / "main.tf").read_text() == source
    assert result.files["main.tf"].startswith("sha256:")
    assert result.outputs["cluster_name"] == ("aws_ecs_cluster.main.name", "string")
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


def test_repair_replaces_only_returned_files_and_preserves_others(monkeypatch, tmp_path):
    root = tmp_path / "bundles"
    previous, current = root / "run-1", root / "run-1-retry-2"
    previous.mkdir(parents=True)
    current.mkdir()
    old_network = 'resource "aws_default_security_group" "main" {}\n'
    old_iam = 'resource "aws_iam_role" "task" {}\n'
    fixed_network = 'resource "aws_vpc" "main" {}\n'
    (previous / "network.tf").write_text(old_network)
    (previous / "iam.tf").write_text(old_iam)
    captured = {}
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")

    def call(**kwargs):
        captured.update(kwargs)
        return draft(**{"network.tf": fixed_network})

    monkeypatch.setattr(logic, "call_ai", call)
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
    assert (current / "iam.tf").read_text() == old_iam
    assert set(result.files) == {"network.tf", "iam.tf"}
    assert captured["instruction"] == logic._REPAIR_INSTRUCTION
    assert captured["instruction"].startswith(logic._REPAIR_PROMPT)
    assert logic._PROMPT in captured["instruction"]
    assert captured["data_max_len"] == logic._MAX_BUNDLE
    assert "validation_error=RESOURCE_NOT_ALLOWED" in captured["data"]


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
