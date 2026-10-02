"""코드 소유 기반 보호 및 앱 buildspec 실행 차단. AWS 호출 없음."""

import json
from unittest.mock import Mock

import pytest

from ddak.cloud.infra.plan import summarize_plan
from ddak.cloud.infra.policy import OVERRIDE_REQUIRED_BUILDSPEC, PolicyViolation, static_gate
from ddak.cloud.infra.providers.aws import start_build_policy
from tests.unit.cloud.infra.test_runtime import ACCOUNT, SETTINGS, resource


def summary(item):
    return summarize_plan(
        {"format_version": "1.2", "resource_changes": [item]},
        layer="platform",
        plan_sha256="sha256:" + "a" * 64,
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=False,
        analyzer=Mock(),
        checkov={"passed": True},
        state_bucket=SETTINGS.state_bucket,
    )


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("aws_s3_bucket", ""),
        ("aws_s3_bucket_versioning", 'versioning_configuration { status = "Suspended" }'),
        ("aws_s3_bucket_public_access_block", "block_public_acls = false"),
    ],
)
def test_foundation_bucket_is_rejected_before_init_and_in_resolved_plan(kind, extra):
    source = f'resource "{kind}" "state" {{ bucket = "{SETTINGS.state_bucket}"\n{extra}\n}}'
    result = static_gate({"main.tf": source}, layer="platform", state_bucket=SETTINGS.state_bucket)
    assert not result.passed and result.detail == "FOUNDATION_BUCKET_OWNED_BY_CODE"
    for action in (["create"], ["update"], ["no-op"]):
        with pytest.raises(PolicyViolation, match="FOUNDATION"):
            summary(resource(kind, "state", action, {"bucket": SETTINGS.state_bucket}))
    with pytest.raises(PolicyViolation, match="FOUNDATION"):
        summary(resource(kind, "state", ["delete"], None, before={"bucket": SETTINGS.state_bucket}))


@pytest.mark.parametrize("spec", [None, "buildspec.yml", "alternate.yml"])
def test_repository_buildspec_is_never_default(spec):
    body = {"source": [{"type": "GITHUB", **({"buildspec": spec} if spec else {})}]}
    attr = "" if spec is None else "buildspec = " + json.dumps(spec)
    source = (
        'resource "aws_codebuild_project" "build" { source { type = "GITHUB"\n' + attr + "\n} }"
    )
    assert not static_gate({"main.tf": source}, layer="platform").passed
    with pytest.raises(PolicyViolation, match="BUILDSPEC"):
        summary(resource("aws_codebuild_project", "build", ["create"], body))


def test_only_platform_override_guard_is_allowed():
    source = (
        'resource "aws_codebuild_project" "build" { source { type = "GITHUB"\nbuildspec = '
        + json.dumps(OVERRIDE_REQUIRED_BUILDSPEC)
        + "\n} }"
    )
    assert static_gate({"main.tf": source}, layer="platform").passed
    result = summary(
        resource(
            "aws_codebuild_project",
            "build",
            ["create"],
            {"source": [{"type": "GITHUB", "buildspec": OVERRIDE_REQUIRED_BUILDSPEC}]},
        )
    )
    assert result["layer"] == "platform"


def test_deployer_startbuild_policy_limits_override_names_and_requires_platform_buildspec():
    policy = start_build_policy(ACCOUNT, "ddak-demo", "https://github.com/team/app")
    statement = policy["Statement"][0]
    condition = statement["Condition"]
    assert condition["ForAllValues:StringEquals"][
        "codebuild:environment.environmentVariables.name"
    ] == ["BUILD_TIERS", "RELEASE_ID", "SOURCE_REVISION", "IMAGE_REPO"]
    assert condition["StringEquals"]["codebuild:source.location"] == "https://github.com/team/app"
    assert condition["Null"]["codebuild:source.buildspec"] == "false"
    assert condition["Null"]["codebuild:serviceRole"] == "true"


def test_unresolved_bucket_is_rejected_before_approval():
    item = resource("aws_s3_bucket_versioning", "other", ["create"], {"bucket": None})
    item["change"]["after_unknown"] = {"bucket": True}
    with pytest.raises(PolicyViolation, match="FOUNDATION_BUCKET_UNRESOLVED"):
        summary(item)


def test_approved_s3_fallback_has_exact_source_policy():
    policy = start_build_policy(
        ACCOUNT,
        "ddak-demo",
        "https://github.com/team/app",
        source_location="fixture-source/approved/source.zip",
    )
    assert (
        policy["Statement"][0]["Condition"]["StringEquals"]["codebuild:source.location"]
        == "fixture-source/approved/source.zip"
    )
