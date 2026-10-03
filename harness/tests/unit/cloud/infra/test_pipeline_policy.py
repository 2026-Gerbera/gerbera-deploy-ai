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
        'resource "aws_codebuild_project" "build" { name = "ddak-demo-build"\n'
        'source { type = "GITHUB"\nbuildspec = ' + json.dumps(OVERRIDE_REQUIRED_BUILDSPEC) + "\n} }"
    )
    assert static_gate({"main.tf": source}, layer="platform").passed
    result = summary(
        resource(
            "aws_codebuild_project",
            "build",
            ["create"],
            {
                "name": "ddak-demo-build",
                "source": [{"type": "GITHUB", "buildspec": OVERRIDE_REQUIRED_BUILDSPEC}],
            },
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


@pytest.mark.parametrize("name", [None, "fixture"])
def test_plan_address_derives_only_omitted_name(name):
    item = resource("aws_vpc", "fixture", ["create"], {})
    if name is not None:
        item["name"] = name
    assert summary(item)["counts"]["create"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "wrong"},
        {"name": None},
        {"name": ""},
        {"address": ""},
        {"address": None},
        {"address": "aws_subnet.fixture"},
        {"address": "module.app.aws_vpc.fixture"},
        {"address": 'aws_vpc.fixture["key"]'},
        {"index": "key"},
        {"index": 0, "address": "aws_vpc.fixture[0]"},
    ],
)
def test_plan_address_mismatch_and_unsupported_indices_fail(changes):
    item = resource("aws_vpc", "fixture", ["no-op"], {})
    item.update(changes)
    with pytest.raises(PolicyViolation, match="PLAN_ADDRESS"):
        summary(item)


def test_plan_missing_address_fails():
    item = resource("aws_vpc", "fixture", ["create"], {})
    item.pop("address")
    with pytest.raises(PolicyViolation, match="PLAN_ADDRESS"):
        summary(item)


@pytest.mark.parametrize("name_present", [True, False])
@pytest.mark.parametrize("domain", ["app.example.com", "*.example.com"])
def test_only_acm_certificate_record_for_each_is_supported(name_present, domain):
    item = resource("aws_route53_record", "certificate_validation", ["create"], {})
    item["address"] += f"[{json.dumps(domain)}]"
    item["index"] = domain
    if name_present:
        item["name"] = "certificate_validation"
    assert summary(item)["counts"]["create"] == 1


@pytest.mark.parametrize(
    "name,index,address",
    [
        ("other", "example.com", 'aws_route53_record.other["example.com"]'),
        (
            "certificate_validation",
            "example.com",
            'aws_route53_record.certificate_validation["wrong.com"]',
        ),
        ("certificate_validation", "example.com", "aws_route53_record.certificate_validation"),
        (
            "certificate_validation",
            "invalid/host",
            'aws_route53_record.certificate_validation["invalid/host"]',
        ),
        ("certificate_validation", 0, "aws_route53_record.certificate_validation[0]"),
    ],
)
def test_for_each_address_mismatch_is_rejected(name, index, address):
    item = resource("aws_route53_record", name, ["create"], {})
    item.update(name=name, index=index, address=address)
    with pytest.raises(PolicyViolation, match="PLAN_ADDRESS"):
        summary(item)


@pytest.mark.parametrize(
    "kind,label,expression,valid",
    [
        (
            "aws_route53_record",
            "certificate_validation",
            "{for dvo in aws_acm_certificate.main.domain_validation_options : "
            "dvo.domain_name => dvo}",
            True,
        ),
        (
            "aws_route53_record",
            "other",
            "{for dvo in aws_acm_certificate.main.domain_validation_options : "
            "dvo.domain_name => dvo}",
            False,
        ),
        (
            "aws_vpc",
            "certificate_validation",
            "{for dvo in aws_acm_certificate.main.domain_validation_options : "
            "dvo.domain_name => dvo}",
            False,
        ),
        ("aws_route53_record", "certificate_validation", '{"a" = "b"}', False),
        (
            "aws_route53_record",
            "certificate_validation",
            "{for dvo in aws_acm_certificate.other.domain_validation_options : "
            "dvo.domain_name => dvo}",
            False,
        ),
        (
            "aws_route53_record",
            "certificate_validation",
            "{for dvo in aws_acm_certificate.main.domain_validation_options : "
            "dvo.domain_name => data.x.value}",
            False,
        ),
    ],
)
def test_hcl_for_each_is_restricted_to_acm_validation(kind, label, expression, valid):
    text = f'resource "{kind}" "{label}" {{ for_each = {expression} }}'
    result = static_gate({"main.tf": text}, layer="platform")
    assert result.passed is valid
    if not valid:
        assert result.detail == "ATTRIBUTE_NOT_ALLOWED"


@pytest.mark.parametrize(
    "name,valid",
    [
        ("ddak-demo-build", True),
        ("ddak-x-build", True),
        ("demo-build", False),
        ("ddak-codebuild", False),
        (None, False),
    ],
)
def test_codebuild_name_rule_is_shared_by_hcl_and_plan(name, valid):
    body = {"source": [{"type": "GITHUB", "buildspec": OVERRIDE_REQUIRED_BUILDSPEC}]}
    attr = ""
    if name is not None:
        body["name"] = name
        attr = "name = " + json.dumps(name) + "\n"
    text = (
        'resource "aws_codebuild_project" "build" {'
        + attr
        + 'source { type = "GITHUB"\nbuildspec = '
        + json.dumps(OVERRIDE_REQUIRED_BUILDSPEC)
        + "\n}}"
    )
    assert static_gate({"main.tf": text}, layer="platform").passed is valid
    item = resource("aws_codebuild_project", "build", ["no-op"], body)
    if valid:
        assert summary(item)
    else:
        with pytest.raises(PolicyViolation, match="CODEBUILD_PROJECT_NAME"):
            summary(item)


def multi_summary(items, configs=(), **kwargs):
    return summarize_plan(
        {
            "format_version": "1.2",
            "resource_changes": items,
            "configuration": {"root_module": {"resources": list(configs)}},
        },
        layer="platform",
        plan_sha256="sha256:" + "a" * 64,
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=False,
        project="flaskr",
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True},
        state_bucket=SETTINGS.state_bucket,
        **kwargs,
    )


def role_items(role_name, secret, *, unknown=False):
    role = resource(
        "aws_iam_role",
        "dbinit_execution",
        ["no-op"],
        {
            "name": role_name,
            "path": "/ddak/app/",
            "permissions_boundary": SETTINGS.boundary_arn,
        },
    )
    policy = resource(
        "aws_iam_role_policy",
        "dbinit",
        ["create"],
        {
            "role": None if unknown else role_name,
            "policy": json.dumps(
                {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["secretsmanager:GetSecretValue"],
                            "Resource": [secret],
                        }
                    ]
                }
            ),
        },
    )
    policy["change"]["after_unknown"] = {"role": unknown}
    config = {
        "type": policy["type"],
        "address": policy["address"],
        "mode": "managed",
        "expressions": {"role": {"references": ["aws_iam_role.dbinit_execution.id"]}},
    }
    return [role, policy], [config] if unknown else []


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize(
    "role_name,requested,valid",
    [
        ("ddak-flaskr-dbinit-exec", "rds!fixture", True),
        ("ddak-flaskr-exec", "rds!fixture", False),
        ("ddak-flaskr-dbinit-exec", "rds!other", False),
        ("ddak-flaskr-dbinit-exec", "rds!*", False),
        ("ddak-flaskr-dbinit-exec", "rds!db-????????-????-????-????-????????????-??????", False),
    ],
)
def test_rds_exact_arn_and_name_preserved_for_resolved_and_direct_roles(
    unknown, role_name, requested, valid
):
    prefix = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:"
    items, configs = role_items(role_name, prefix + requested, unknown=unknown)
    if valid:
        assert multi_summary(items, configs, rds_master_secret_arn=prefix + "rds!fixture")
    else:
        with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
            multi_summary(items, configs, rds_master_secret_arn=prefix + "rds!fixture")


def test_rds_resolved_name_path_does_not_require_special_terraform_label():
    secret = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!fixture"
    items, _ = role_items("ddak-flaskr-dbinit-exec", secret)
    items[0]["address"] = "aws_iam_role.exec"
    assert multi_summary(items, rds_master_secret_arn=secret)


@pytest.mark.parametrize(
    "refs",
    [
        [],
        ["aws_iam_role.missing.id"],
        ["aws_iam_role.dbinit_execution.id", "var.role"],
        ["aws_iam_role.dbinit_execution.id", "aws_iam_role.other.id"],
    ],
)
def test_unknown_role_requires_verified_direct_address(refs):
    secret = f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:rds!fixture"
    items, configs = role_items("ddak-flaskr-dbinit-exec", secret, unknown=True)
    configs[0]["expressions"]["role"] = {"references": refs}
    with pytest.raises(PolicyViolation, match="IAM_ROLE_UNKNOWN"):
        multi_summary(items, configs, rds_master_secret_arn=secret)


def bucket_items(bucket):
    base = resource("aws_s3_bucket", "artifacts", ["create"], {"bucket": bucket})
    child = resource("aws_s3_bucket_versioning", "artifacts", ["create"], {"bucket": None})
    child["change"]["after_unknown"] = {"bucket": True}
    config = {
        "address": child["address"],
        "type": child["type"],
        "mode": "managed",
        "expressions": {"bucket": {"references": ["aws_s3_bucket.artifacts.id"]}},
    }
    return [base, child], [config]


def test_unknown_bucket_resolves_only_non_foundation_direct_reference():
    items, configs = bucket_items("ddak-fixture-artifacts")
    assert multi_summary(items, configs)["counts"]["create"] == 2


@pytest.mark.parametrize(
    "failure", ["foundation", "old-foundation", "unknown-target", "missing-target", "extra-ref"]
)
def test_unknown_bucket_does_not_bypass_foundation_protection(failure):
    items, configs = bucket_items("ddak-fixture-artifacts")
    if failure == "foundation":
        items[0]["change"]["after"]["bucket"] = SETTINGS.state_bucket
    elif failure == "old-foundation":
        items[1]["change"]["before"] = {"bucket": SETTINGS.state_bucket}
    elif failure == "unknown-target":
        items[0]["change"]["after_unknown"] = {"bucket": True}
    elif failure == "missing-target":
        items.pop(0)
    elif failure == "extra-ref":
        configs[0]["expressions"]["bucket"]["references"].append("var.suffix")
    with pytest.raises(PolicyViolation, match="FOUNDATION"):
        multi_summary(items, configs)


@pytest.mark.parametrize(
    "name,valid",
    [
        ("ddak-${var.project}-build", True),
        ("ddak-${var.account_id}-build", False),
        ("ddak-${var.project}-other", False),
        ("ddak-prefix-${var.project}-build", False),
        ("ddak-${var.project}-build-extra", False),
    ],
)
def test_codebuild_exact_project_template_is_hcl_only(name, valid):
    body = {"name": name, "source": [{"type": "GITHUB", "buildspec": OVERRIDE_REQUIRED_BUILDSPEC}]}
    text = (
        'resource "aws_codebuild_project" "build" { name = '
        + json.dumps(name)
        + '\nsource { type = "GITHUB"\nbuildspec = '
        + json.dumps(OVERRIDE_REQUIRED_BUILDSPEC)
        + "\n}}"
    )
    result = static_gate({"main.tf": text}, layer="platform")
    assert result.passed is valid
    if not valid:
        assert result.detail == "CODEBUILD_PROJECT_NAME_INVALID"
    for action in (["create"], ["no-op"]):
        with pytest.raises(PolicyViolation, match="CODEBUILD_PROJECT_NAME_INVALID"):
            summary(resource("aws_codebuild_project", "build", action, body))
