"""대표 rolling fixture의 생성·출력 참조·정책 계약. 실제 AI/AWS/Terraform은 실행하지 않는다."""

import json
import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from hcl2 import loads

from ddak.cloud.infra.plan import filter_outputs, summarize_plan
from ddak.cloud.infra.policy import PolicyViolation, static_gate
from ddak.cloud.infra.runtime import AwsSettings
from ddak.cloud.infra.tools.generate_infra import logic
from ddak.core import runtime
from ddak.core.ai import gateway
from ddak.core.ai.providers import AIResponse
from ddak.core.contracts.enums import Source
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput
from ddak.core.snapshots import digest_bytes
from tests.unit.cloud.infra.test_generate_infra import context

FIXTURE = Path(__file__).parents[4] / "fixtures" / "infra" / "rolling_v3"


def fixture_data():
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(FIXTURE.glob("*.tf"))}
    plan = json.loads((FIXTURE / "plan.json").read_text(encoding="utf-8"))
    metadata = json.loads((FIXTURE / "metadata.json").read_text(encoding="utf-8"))
    return files, plan, metadata


def hcl_resources(files):
    resources = {}
    for source in files.values():
        for item in loads(source)["resource"]:
            for kind, named in item.items():
                for label, body in named.items():
                    address = f"{kind}.{label}"
                    assert address not in resources
                    resources[address] = body
    return resources


def plan_resources(plan):
    # 하나뿐인 ACM DNS 검증 인스턴스는 HCL의 for_each 기본 주소와 대응한다.
    return {
        f"{item['type']}.{item['name']}": item["change"]["after"]
        for item in plan["resource_changes"]
    }


def summarize(plan, metadata):
    analyzer = Mock(validate_policy=Mock(return_value={"findings": []}))
    result = summarize_plan(
        plan,
        layer="platform",
        plan_sha256=digest_bytes(json.dumps(plan, sort_keys=True).encode()),
        exit_code=2,
        account_id=metadata["account_id"],
        boundary_arn=metadata["app_boundary_arn"],
        build_boundary_arn=metadata["build_boundary_arn"],
        project=metadata["project"],
        update=False,
        analyzer=analyzer,
        # 검사기 수행 결과가 아니라 요약 게이트의 입력 fixture다.
        checkov={"passed": True, "source": metadata["source"]},
    )
    assert analyzer.validate_policy.called
    return result


def resolve_outputs(outputs, declared, planned):
    """실제 생성기 표현식을 선언 주소와 synthetic plan 속성에서 해석한다."""

    def reference(expression):
        kind, label, attribute = expression.strip().split(".", 2)
        address = f"{kind}.{label}"
        assert address in declared, f"undeclared output target: {address}"
        assert address in planned, f"missing plan output target: {address}"
        value = planned[address]
        for field in attribute.split("."):
            match = re.fullmatch(r"([a-z_]+)(?:\[([0-9]+)\])?", field)
            assert match, field
            value = value[match[1]]
            if match[2] is not None:
                value = value[int(match[2])]
        return value

    resolved = {}
    for name, (expression, kind) in outputs.items():
        if expression.startswith('"'):
            value = json.loads(expression)
        elif kind == "list(string)":
            assert expression.startswith("[") and expression.endswith("]")
            value = [reference(item) for item in expression[1:-1].split(",")]
        else:
            value = reference(expression)
        resolved[name] = {"value": value, "sensitive": False}
    return filter_outputs(resolved, {name: kind for name, (_, kind) in outputs.items()})


@pytest.fixture
def fixture_provider(monkeypatch):
    files, _, metadata = fixture_data()
    draft = logic.TerraformDraft(
        files=tuple(
            logic.TerraformFileDraft(name=name, lines=tuple(source.splitlines()))
            for name, source in files.items()
        )
    )
    requests = []

    class FixtureProvider:
        def complete(self, request):
            requests.append(request)
            return AIResponse(text=draft.model_dump_json(), source=Source.FIXTURE)

    monkeypatch.setattr(gateway, "get_provider", lambda _settings: FixtureProvider())
    monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", metadata["dockerhub_namespace"])
    token = runtime.current_tool.set("generate_infra")
    try:
        assert logic.call_ai is gateway.call_ai
        yield requests
    finally:
        runtime.current_tool.reset(token)


def generate(directory):
    directory.mkdir(parents=True)
    return logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(directory), layer="platform"), context()
    )


def test_fixture_generation_passes_both_gates_and_resolves_actual_outputs(
    fixture_provider, tmp_path
):
    files, plan, metadata = fixture_data()
    generated = generate(tmp_path / "bundles" / "run-1")
    written = {
        name: (Path(generated.directory) / name).read_text(encoding="utf-8")
        for name in generated.files
    }
    assert written == files
    assert generated.files == {
        name: digest_bytes(source.encode()) for name, source in written.items()
    }
    assert generated.source is Source.FIXTURE
    assert metadata["source"] == "fixture"
    assert metadata["prompt_version"] == logic.PROMPT_VERSION == "infra-aws-v3-rolling"
    assert metadata["upstream_sha"] == "c4af8656c2a6b574311f37fe3f12d0272c99e66d"
    assert fixture_provider[0].prompt_version == metadata["prompt_version"]
    assert fixture_provider[0].user.startswith(logic._PROMPT)
    # 교정은 동일한 rolling 계약을 포함하므로 별도 repair prompt 수정이 필요 없다.
    assert logic._PROMPT in logic._REPAIR_INSTRUCTION

    gate = static_gate(written, layer="platform")
    assert gate.passed, gate.detail
    summary = summarize(plan, metadata)
    assert summary["counts"]["create"] == len(plan["resource_changes"])
    assert summary["destructive"] == []
    assert len(summary["iam_diff"]) == 6  # 역할 4개 + 별도 정책 2개; template/output 중복 없음.
    summary_bytes = len(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode())
    assert summary_bytes <= 8192, f"representative rolling summary: {summary_bytes} bytes"

    declared, planned = hcl_resources(written), plan_resources(plan)
    assert declared.keys() == planned.keys()
    resolved = resolve_outputs(generated.outputs, declared, planned)
    assert resolved["image_repository"] == "example/flaskr"
    assert generated.outputs["app_secret_arn_DATABASE_URL"] == (
        "aws_secretsmanager_secret.app_database_url.arn",
        "string",
    )
    assert (
        resolved["app_secret_arn_DATABASE_URL"]
        == planned["aws_secretsmanager_secret.app_database_url"]["arn"]
    )
    assert resolved["public_subnet_ids"] == ["subnet-public_a", "subnet-public_b"]
    assert (
        resolved["rds_master_secret_arn"]
        == planned["aws_db_instance.main"]["master_user_secret"][0]["secret_arn"]
    )

    framework = AwsSettings(
        project=metadata["project"],
        account_id=metadata["account_id"],
        state_bucket="ddak-fixture-state",
        layer=generated.layer,
        outputs=generated.outputs,
    ).framework()
    assert framework["output"] == {
        name: {"value": resolved[name] if name == "image_repository" else "${" + expression + "}"}
        for name, (expression, _) in generated.outputs.items()
    }


def test_fixture_has_one_rolling_target_with_exact_scoped_contract():
    files, plan, metadata = fixture_data()
    declared, planned = hcl_resources(files), plan_resources(plan)
    assert metadata["deployment_strategy"] == "ROLLING"
    assert metadata["target_group_count"] == 1
    health = {
        "interval": 5,
        "timeout": 3,
        "healthy_threshold": 2,
        "unhealthy_threshold": 2,
        "path": "/health/ready",
        "matcher": "200",
    }
    for resources, project in ((declared, "${var.project}"), (planned, "flaskr")):
        assert [a for a in resources if a.startswith("aws_lb_target_group.")] == [
            "aws_lb_target_group.app"
        ]
        assert not any(a.startswith("aws_lb_listener_rule.") for a in resources)
        target = resources["aws_lb_target_group.app"]
        assert int(target["deregistration_delay"]) == 30
        assert target["health_check"] == [health]
        https = resources["aws_lb_listener.https"]
        assert https["ssl_policy"] == "ELBSecurityPolicy-TLS13-1-2-2021-06"
        target_arn = "${aws_lb_target_group.app.arn}" if resources is declared else target["arn"]
        assert https["default_action"] == [{"type": "forward", "target_group_arn": target_arn}]
        service = resources["aws_ecs_service.app"]
        assert "deployment_configuration" not in service
        assert service["load_balancer"] == [
            {"target_group_arn": target_arn, "container_name": "web", "container_port": 8080}
        ]
        assert service["deployment_minimum_healthy_percent"] == 100
        assert service["deployment_maximum_percent"] == 200
        assert service["deployment_circuit_breaker"] == [{"enable": True, "rollback": True}]
        assert resources["aws_cloudwatch_log_group.app"]["name"] == f"/aws/ecs/{project}"
        secret = resources["aws_secretsmanager_secret.app_database_url"]
        assert secret["name"] == f"ddak/{project}/DATABASE_URL"
        assert not {"secret_string", "secret_binary", "secret_string_wo"} & secret.keys()
        assert not any(a.startswith("aws_secretsmanager_secret_version.") for a in resources)

        rules = {
            a: body
            for a, body in resources.items()
            if a.startswith("aws_vpc_security_group_egress_rule.")
        }
        assert set(rules) == {
            f"aws_vpc_security_group_egress_rule.{name}"
            for name in ("alb_app", "app_db", "app_https", "app_dns_tcp", "app_dns_udp")
        }
        sg = {
            label: f"${{aws_security_group.{label}.id}}"
            if resources is declared
            else resources[f"aws_security_group.{label}"]["id"]
            for label in ("alb", "app", "db")
        }
        for name, owner, protocol, port, destination in (
            ("alb_app", "alb", "tcp", 8080, {"referenced_security_group_id": sg["app"]}),
            ("app_db", "app", "tcp", 3306, {"referenced_security_group_id": sg["db"]}),
            ("app_https", "app", "tcp", 443, {"cidr_ipv4": "0.0.0.0/0"}),
            ("app_dns_tcp", "app", "tcp", 53, {"cidr_ipv4": "10.40.0.2/32"}),
            ("app_dns_udp", "app", "udp", 53, {"cidr_ipv4": "10.40.0.2/32"}),
        ):
            assert rules[f"aws_vpc_security_group_egress_rule.{name}"] == {
                "security_group_id": sg[owner],
                "ip_protocol": protocol,
                "from_port": port,
                "to_port": port,
                **destination,
            }
        for label in ("alb", "app", "db"):
            assert resources[f"aws_security_group.{label}"].get("egress", []) == []


@pytest.mark.parametrize("action", ["create", "update", "no-op"])
@pytest.mark.parametrize(
    "old,new,path,value,rule",
    [
        (
            "deregistration_delay = 30",
            "deregistration_delay = 300",
            ("deregistration_delay",),
            "300",
            "TARGET_GROUP_DEREGISTRATION",
        ),
        (
            "interval = 5",
            "interval = 30",
            ("health_check", 0, "interval"),
            30,
            "TARGET_GROUP_INTERVAL",
        ),
    ],
)
def test_rolling_fixture_speed_mutations_fail_hcl_and_plan(old, new, path, value, rule, action):
    files, plan, metadata = fixture_data()
    # 먼저 원본이 통과해야 변형 거부가 다른 fixture 오류에 가려지지 않는다.
    gate = static_gate(files, layer="platform")
    assert gate.passed, gate.detail
    assert summarize(plan, metadata)
    assert files["application.tf"].count(old) == 1
    files["application.tf"] = files["application.tf"].replace(old, new, 1)
    gate = static_gate(files, layer="platform")
    assert not gate.passed and gate.detail == rule

    mutated = deepcopy(plan)
    target = next(
        item for item in mutated["resource_changes"] if item["address"] == "aws_lb_target_group.app"
    )
    target["change"]["actions"] = [action]
    body = target["change"]["after"]
    for key in path[:-1]:
        body = body[key]
    body[path[-1]] = value
    with pytest.raises(PolicyViolation, match=rule):
        summarize(mutated, metadata)


@pytest.mark.parametrize("missing_from", ["hcl", "plan"])
def test_actual_database_url_output_cannot_resolve_a_missing_secret(
    fixture_provider, tmp_path, missing_from
):
    files, plan, _ = fixture_data()
    generated = generate(tmp_path / "bundles" / "run-1")
    declared, planned = hcl_resources(files), plan_resources(plan)
    (declared if missing_from == "hcl" else planned).pop(
        "aws_secretsmanager_secret.app_database_url"
    )
    with pytest.raises(
        AssertionError, match=r"output target: aws_secretsmanager_secret\.app_database_url"
    ):
        resolve_outputs(generated.outputs, declared, planned)


@pytest.mark.parametrize("old_version", [None, "infra-aws-v2"])
def test_unversioned_or_old_baseline_cannot_bypass_new_prompt(
    fixture_provider, tmp_path, old_version
):
    root = tmp_path / "infra-baselines" / "flaskr"
    if old_version is not None:
        root /= old_version
    root.mkdir(parents=True)
    for name in ("legacy.tf", "network.tf"):
        (root / name).write_text('resource "aws_vpc" "legacy" {}\n', encoding="utf-8")
    generated = generate(tmp_path / "bundles" / "run-1")
    assert generated.source is Source.FIXTURE
    assert len(fixture_provider) == 1
    assert fixture_provider[0].prompt_version == "infra-aws-v3-rolling"
    assert "legacy.tf" not in generated.files


def test_matching_version_baseline_keeps_cache_source_and_output_references(
    fixture_provider, tmp_path
):
    files, plan, metadata = fixture_data()
    root = tmp_path / "infra-baselines" / "flaskr" / metadata["prompt_version"]
    root.mkdir(parents=True)
    for name, source in files.items():
        (root / name).write_text(source, encoding="utf-8")
    generated = generate(tmp_path / "bundles" / "run-1")
    assert generated.source is Source.CACHE
    assert not fixture_provider
    written = {
        name: (Path(generated.directory) / name).read_text(encoding="utf-8")
        for name in generated.files
    }
    assert written == files
    gate = static_gate(written, layer="platform")
    assert gate.passed, gate.detail
    assert summarize(plan, metadata)
    assert resolve_outputs(generated.outputs, hcl_resources(written), plan_resources(plan))
