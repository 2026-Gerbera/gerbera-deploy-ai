"""source=fixture: 클라우드 플랫폼 이름 별칭. 실제 AWS·Terraform·AI 호출 없음."""

from hashlib import sha256
from unittest.mock import Mock

import pytest

from ddak.cloud.infra import assembly, bindings
from ddak.cloud.infra.fixture import fixture_binding
from ddak.cloud.infra.runtime import AwsSettings, check_approval
from ddak.cloud.infra.tools.generate_infra import logic
from ddak.core.config import AdapterMode
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.project_settings import cloud_platform_name
from ddak.core.snapshots import digest_bytes
from tests.unit.cloud.infra.test_generate_infra import context as generate_context
from tests.unit.cloud.infra.test_generate_infra import draft
from tests.unit.cloud.test_aws_credentials import ACCOUNT, SELECTION

PROJECT = "flaskr-three"
REPOSITORY = "2026gerbera/flaskr"


def test_platform_name_defaults_to_project_and_rejects_bad_alias():
    assert cloud_platform_name(PROJECT, {}) == PROJECT
    assert cloud_platform_name(PROJECT, {"cloud_platform": None}) == PROJECT
    assert cloud_platform_name(PROJECT, {"cloud_platform": "flaskr"}) == "flaskr"
    for bad in ("Flaskr", "flaskr_x", "../flaskr", "1flaskr", "a" * 41, "", 3):
        with pytest.raises(DdakToolError) as exc:
            cloud_platform_name(PROJECT, {"cloud_platform": bad})
        assert exc.value.code is ErrorCode.CONFIG_INVALID


def _aws_settings(monkeypatch, tmp_path, project_settings) -> AwsSettings:
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    frozen = Mock(access_key="fixture-access", secret_key="fixture-" + "value", token=None)

    def make_session(**kwargs):
        session = Mock()
        session.get_credentials.return_value.get_frozen_credentials.return_value = frozen
        session.client.return_value = sts
        return session

    monkeypatch.setattr(assembly.boto3, "Session", Mock(side_effect=make_session))
    runtime = Mock()
    monkeypatch.setattr(assembly, "InfraRuntime", runtime)
    source = b'resource "aws_s3_bucket" "example" {}\n'
    bundle = GenerateInfraOutput(
        directory=str(tmp_path), layer="app", files={"main.tf": digest_bytes(source)}
    )
    ctx = RunContext(
        "run-alias",
        adapter_mode=AdapterMode.REAL,
        project=PROJECT,
        project_settings={**SELECTION, **project_settings},
    )
    assembly.create_binding(
        bundle,
        {"main.tf": source.decode()},
        ctx,
        root=tmp_path,
        approvals=lambda: (),
        guard=lambda: None,
    )
    return runtime.call_args.kwargs["settings"]


@pytest.mark.parametrize(
    "saved,platform",
    [({}, PROJECT), ({"cloud_platform": None}, PROJECT), ({"cloud_platform": "flaskr"}, "flaskr")],
)
def test_state_bucket_key_and_names_follow_platform(monkeypatch, tmp_path, saved, platform):
    aws = _aws_settings(monkeypatch, tmp_path, saved)
    assert aws.project == platform
    assert aws.run_project == PROJECT
    assert aws.state_bucket == f"ddak-state-{ACCOUNT}-{sha256(platform.encode()).hexdigest()[:16]}"
    assert aws.backend()["key"] == f"ddak/{platform}/app.tfstate"
    framework = aws.framework()
    assert framework["variable"]["project"]["default"] == platform
    assert framework["provider"]["aws"]["default_tags"]["tags"]["Project"] == platform


def test_existing_flaskr_bucket_suffix_is_reused():
    # 기존 플랫폼 bucket ddak-state-<account>-09a143cca5dfea7b와 같은 규칙이다.
    assert sha256(b"flaskr").hexdigest()[:16] == "09a143cca5dfea7b"


def test_approvals_match_project_name_not_platform():
    aws = AwsSettings("flaskr", ACCOUNT, "ddak-fixture-state", "app", {}, approval_project=PROJECT)

    def record(project: str) -> ApprovalRecord:
        return ApprovalRecord(
            run_id="run-alias",
            project=project,
            approval_id="a" * 32,
            kind="infra",
            bound_to="sha256:" + "1" * 64,
            approver="operator",
            approved_at="2026-10-03T00:00:00Z",
            decision="approved",
        )

    subject = {"run_id": "run-alias", "kind": "infra", "bound_to": "sha256:" + "1" * 64}
    check_approval([record(PROJECT)], project=aws.run_project, **subject)
    with pytest.raises(DdakToolError) as exc:
        check_approval([record("flaskr")], project=aws.run_project, **subject)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    with pytest.raises(DdakToolError):
        AwsSettings("flaskr", ACCOUNT, "ddak-fixture-state", "app", {}, approval_project="Bad")


def test_platform_change_changes_bootstrap_approval_material():
    # bootstrap 승인 해시는 backend(bucket·key)를 포함하고, 갱신 plan 파일에도 backend가 들어간다.
    old = AwsSettings(PROJECT, ACCOUNT, "ddak-state-a", "platform", {})
    new = AwsSettings("flaskr", ACCOUNT, "ddak-state-b", "platform", {}, approval_project=PROJECT)
    assert old.backend() != new.backend()
    assert old.framework() != new.framework()


def _fake_context(**project_settings) -> RunContext:
    return RunContext(
        "run-fixture-alias",
        adapter_mode=AdapterMode.FAKE,
        project=PROJECT,
        mode=RunMode.BOOTSTRAP,
        project_settings=project_settings,
    )


def test_fixture_binding_and_session_check_use_platform(tmp_path):
    ctx = _fake_context(cloud_platform="flaskr")
    binding = fixture_binding(ctx, root=tmp_path, approvals=lambda: [], guard=lambda: None)
    assert binding.runtime.settings.project == "flaskr"
    assert binding.runtime.settings.run_project == PROJECT
    assert "ddak-flaskr-fixture" in binding.files["main.tf"]
    bindings.bind_infra(binding)
    try:
        assert bindings._binding(ctx.run_id, ctx) is not None
        with pytest.raises(DdakToolError) as exc:
            bindings._binding(ctx.run_id, _fake_context(cloud_platform="other"))
        assert exc.value.code is ErrorCode.INFRA_MISSING
        with pytest.raises(DdakToolError):
            bindings._binding(ctx.run_id, _fake_context())
    finally:
        bindings.unbind_infra(ctx.run_id)


def _generate_context(**settings) -> RunContext:
    base = generate_context()
    return RunContext(
        base.run_id,
        adapter_mode=base.adapter_mode,
        project=PROJECT,
        mode=base.mode,
        repo_url=base.repo_url,
        cloud_domain=base.cloud_domain,
        project_settings={**base.project_settings, **settings},
    )


def _bundle_dir(tmp_path):
    directory = tmp_path / "infra-bundles" / "run-1"
    directory.mkdir(parents=True)
    return directory


def _baseline(tmp_path, platform: str) -> None:
    root = tmp_path / "infra-baselines" / platform / logic.PROMPT_VERSION
    root.mkdir(parents=True)
    for name in ("main.tf", "network.tf"):
        (root / name).write_text(f'resource "aws_vpc" "{name[:-3]}" {{}}\n', encoding="utf-8")


@pytest.mark.parametrize("alias,platform", [(None, PROJECT), ("flaskr", "flaskr")])
def test_generate_infra_baseline_and_outputs_follow_platform(
    monkeypatch, tmp_path, alias, platform
):
    monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    monkeypatch.setattr(
        logic, "call_ai", lambda **_: pytest.fail("기준본이 있으면 AI를 부르지 않는다")
    )
    _baseline(tmp_path, platform)
    directory = _bundle_dir(tmp_path)
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(directory), layer="platform"),
        _generate_context(cloud_platform=alias, image_repository=REPOSITORY),
    )
    assert result.source is Source.CACHE
    assert set(result.files) == {"main.tf", "network.tf"}
    assert result.outputs["image_repository"] == (f'"2026gerbera/{platform}"', "string")


def test_generate_infra_ai_input_uses_platform_name(monkeypatch, tmp_path):
    monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    seen = []

    def fake_call_ai(**kwargs):
        seen.append(kwargs["data"])
        return draft(**{"main.tf": 'resource "aws_vpc" "main" {}\n'})

    monkeypatch.setattr(logic, "call_ai", fake_call_ai)
    result = logic.generate_infra(
        GenerateInfraInput(run_id="run-1", directory=str(_bundle_dir(tmp_path)), layer="platform"),
        _generate_context(cloud_platform="flaskr", image_repository=REPOSITORY),
    )
    assert seen[0].startswith("project=flaskr\n")
    assert "dockerhub_namespace=2026gerbera\n" in seen[0]
    assert PROJECT not in seen[0]
    assert result.outputs["image_repository"] == ('"2026gerbera/flaskr"', "string")


@pytest.mark.parametrize(
    "env,settings,ctx_repository,expected",
    [
        ("envns", {"image_repository": REPOSITORY}, None, "envns"),
        (None, {"image_repository": REPOSITORY}, None, "2026gerbera"),
        (None, {}, "localns/app", "localns"),
    ],
)
def test_namespace_prefers_environment_then_image_repository(
    monkeypatch, env, settings, ctx_repository, expected
):
    if env is None:
        monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    else:
        monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", env)
    ctx = RunContext(
        "run-1", project=PROJECT, image_repository=ctx_repository, project_settings=settings
    )
    assert logic._dockerhub_namespace(ctx) == expected


@pytest.mark.parametrize(
    "env,repository",
    [(None, None), (None, "registry.example.com/team"), ("Bad NS", REPOSITORY)],
)
def test_namespace_missing_or_invalid_points_to_admin_page(monkeypatch, env, repository):
    if env is None:
        monkeypatch.delenv("DDAK_DOCKERHUB_NAMESPACE", raising=False)
    else:
        monkeypatch.setenv("DDAK_DOCKERHUB_NAMESPACE", env)
    ctx = RunContext("run-1", project=PROJECT, project_settings={"image_repository": repository})
    with pytest.raises(DdakToolError) as exc:
        logic._dockerhub_namespace(ctx)
    assert exc.value.code is ErrorCode.CONFIG_INVALID
    assert "관리 페이지" in str(exc.value) and "DDAK_DOCKERHUB_NAMESPACE" in str(exc.value)


def _boundary_snapshot(aws: AwsSettings, project: str) -> list[dict]:
    from ddak.cloud.infra.providers.aws import boundary_document, build_boundary_document
    from ddak.cloud.infra.runtime import canonical, digest

    documents = [boundary_document(ACCOUNT, project), build_boundary_document(ACCOUNT)]
    return [
        {
            "policy_arn": arn,
            "default_version_id": "v1",
            "document_sha256": digest(canonical(document)),
            "document": document,
        }
        for arn, document in zip((aws.boundary_arn, aws.build_boundary_arn), documents, strict=True)
    ]


def test_alias_keeps_existing_flaskr_boundary_bucket_tag_and_log_scope(monkeypatch, tmp_path):
    from ddak.cloud.infra.boundary_versions import boundary_changes
    from ddak.cloud.infra.foundation import foundation_template

    aws = _aws_settings(monkeypatch, tmp_path, {"cloud_platform": "flaskr"})
    assert aws.state_bucket == f"ddak-state-{ACCOUNT}-09a143cca5dfea7b"
    assert aws.backend()["key"] == "ddak/flaskr/app.tfstate"
    existing = _boundary_snapshot(aws, "flaskr")
    assert [c["action"] for c in boundary_changes(aws, existing)] == ["unchanged", "unchanged"]
    assert "log-group:/aws/ecs/flaskr:*" in str(foundation_template(aws)["boundary"])
    assert foundation_template(aws)["tags"] == {"ManagedBy": "ddak", "Project": "flaskr"}

    # 별칭이 빠지면 경계 새 버전이 생기고 flaskr 로그 권한이 빠진다(회귀 확인용).
    unaliased = _aws_settings(monkeypatch, tmp_path, {})
    change = boundary_changes(unaliased, existing)[0]
    assert change["action"] == "update"
    assert any("/aws/ecs/flaskr:" in value for value in change["removed_resources"])


def test_cloud_database_url_uses_platform_name():
    from ddak.cloud.deploy import entry

    secret = "arn:aws:secretsmanager:ap-northeast-2:" + ACCOUNT + ":secret:rds!db-x"
    client = Mock()
    client.get_secret_value.return_value = {
        "SecretString": '{"username": "admin", "password": "fixture-' + 'pw"}'
    }
    platform = {"cloud": {"rds_master_secret_arn": secret, "rds_endpoint": "db.example:3306"}}
    for settings, database in (({"cloud_platform": "flaskr"}, "flaskr"), ({}, PROJECT)):
        ctx = RunContext("run-1", project=PROJECT, project_settings=settings, platform=platform)
        url = entry._database_url(client, ctx)
        assert url.split("@db.example:3306/")[1].split("?")[0] == database


@pytest.mark.parametrize("approved_project,passes", [(PROJECT, True), ("flaskr", False)])
def test_fake_bootstrap_apply_checks_approval_by_project_name(tmp_path, approved_project, passes):
    from datetime import UTC, datetime

    from ddak.core.contracts.tools.apply_infra import ApplyInfraInput
    from ddak.core.contracts.tools.plan_infra import PlanInfraInput
    from ddak.core.contracts.tools.validate_infra import ValidateInfraInput

    approvals: list[ApprovalRecord] = []
    ctx = RunContext(
        "run-fake-apply",
        adapter_mode=AdapterMode.FAKE,
        project=PROJECT,
        mode=RunMode.BOOTSTRAP,
        lock_token="fixture-lock",
        project_settings={"cloud_platform": "flaskr"},
    )
    binding = fixture_binding(ctx, root=tmp_path, approvals=lambda: approvals, guard=lambda: None)
    bindings.bind_infra(binding)
    try:
        bindings.run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx)
        planned = bindings.run_plan(PlanInfraInput(run_id=ctx.run_id), ctx)
        assert "클라우드 플랫폼 flaskr" in planned.summary["headline"]
        approvals.append(
            ApprovalRecord(
                run_id=ctx.run_id,
                project=approved_project,
                approval_id="b" * 32,
                kind="infra",
                bound_to=planned.plan_sha256,
                approver="operator",
                approved_at=datetime.now(UTC),
                decision="approved",
            )
        )
        apply = ApplyInfraInput(run_id=ctx.run_id, lock_token="fixture-lock")
        if passes:
            assert bindings.run_apply(apply, ctx).passed is True
        else:
            with pytest.raises(DdakToolError) as exc:
                bindings.run_apply(apply, ctx)
            assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    finally:
        bindings.unbind_infra(ctx.run_id)
