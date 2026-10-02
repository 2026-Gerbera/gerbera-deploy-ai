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


def test_generate_platform_writes_bound_files(monkeypatch, tmp_path):
    source = (
        'resource "aws_secretsmanager_secret" "app_secret_key" {\n'
        '  name = "ddak/${var.project}/SECRET_KEY"\n}\n'
    )
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", "example")
    monkeypatch.setattr(
        logic,
        "call_ai",
        lambda **_kwargs: SimpleNamespace(
            value=logic.TerraformDraft(files={"main.tf": source}), source=Source.LIVE
        ),
    )

    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"),
        context(),
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


def test_generate_requires_route53_and_namespace(monkeypatch, tmp_path):
    monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    with pytest.raises(DdakToolError, match="DDAK_DOCKERHUB_NAMESPACE"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="platform"),
            context(),
        )


def test_app_layer_stops_until_new_secret_contract_exists(tmp_path):
    with pytest.raises(DdakToolError, match="새 시크릿 키 목록 계약"):
        logic.generate_infra(
            GenerateInfraInput(run_id="run-1", directory=str(tmp_path), layer="app"), context()
        )
