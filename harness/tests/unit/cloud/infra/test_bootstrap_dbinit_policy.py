"""bootstrap dbinit 와일드카드의 역할·승인 경계. 실제 SDK/CLI를 호출하지 않는다."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from hcl2.api import loads
from jinja2 import Environment, FileSystemLoader

from ddak.cloud.infra import foundation
from ddak.cloud.infra.plan import _masked, summarize_plan
from ddak.cloud.infra.policy import PolicyViolation, policy_json, static_gate
from ddak.cloud.infra.providers.aws import boundary_document
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.executor.approval_meta import encode_meta
from tests.unit.cloud.infra import test_bluegreen_foundation as b
from tests.unit.cloud.infra import test_runtime as f

ARN = f"arn:aws:secretsmanager:ap-northeast-2:{f.ACCOUNT}:secret:rds!db-*"
MASKED = "RDS bootstrap 범위 rds!db-* (ap-northeast-2, 계정 ************)"
ACTIONS = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
ADDRESS = "aws_iam_role.dbinit_execution"


@pytest.fixture(autouse=True)
def prohibit_real_sdk_and_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 SDK/CLI 호출 금지")

    monkeypatch.setattr("boto3.Session", forbidden)
    monkeypatch.setattr(f.CommandRunner, "run", forbidden)


def raw_plan(*, actions=None, refs=None, unknown=False):
    raw = f.plan_json()
    role = raw["resource_changes"][0]
    role["address"] = ADDRESS
    role["change"]["after"]["name"] = "ddak-flaskr-dbinit-exec"
    policy = raw["resource_changes"][2]
    policy["change"]["after"] = {
        "role": None if unknown else "ddak-flaskr-dbinit-exec",
        "policy": json.dumps(
            {
                "Statement": [
                    {"Effect": "Allow", "Action": actions or ACTIONS, "Resource": refs or [ARN]}
                ]
            }
        ),
    }
    if unknown:
        policy["change"]["after_unknown"] = {"role": True}
        raw["configuration"] = {
            "root_module": {
                "resources": [
                    {
                        "type": "aws_iam_role_policy",
                        "address": "aws_iam_role_policy.read",
                        "expressions": {"role": {"references": [ADDRESS + ".id", ADDRESS]}},
                    }
                ]
            }
        }
    return raw


def summarize(raw, **overrides):
    kwargs = dict(
        layer="platform",
        update=False,
        bootstrap_prepared=True,
        plan_sha256=f.digest(b"fixture"),
        exit_code=2,
        account_id=f.ACCOUNT,
        boundary_arn=f.SETTINGS.boundary_arn,
        project="flaskr",
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True, "failed": []},
    )
    kwargs.update(overrides)
    return summarize_plan(raw, **kwargs)


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("actions", [ACTIONS, ACTIONS[:1], ACTIONS[1:]])
def test_bootstrap_dbinit_read_actions_pass_and_display_scope(unknown, actions):
    summary = summarize(raw_plan(actions=actions, unknown=unknown))
    row = summary["iam_diff"][0]
    assert row["proposed_allow"] == [{"actions": actions, "resources": [MASKED]}]
    assert row["after"][0]["Resource"] == [MASKED]
    encoded = encode_meta(summary, infra=True)
    assert f.ACCOUNT not in encoded
    assert MASKED in encoded


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("noop", [False, True])
def test_other_role_address_rejected_even_with_dbinit_name(unknown, noop):
    raw = raw_plan(unknown=unknown)
    raw["resource_changes"][0]["address"] = "aws_iam_role.app_execution"
    if unknown:
        raw["configuration"]["root_module"]["resources"][0]["expressions"]["role"] = {
            "references": ["aws_iam_role.app_execution.id"]
        }
    if noop:
        raw["resource_changes"][2]["change"]["actions"] = ["no-op"]
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize(raw)


@pytest.mark.parametrize("suffix", ["rds!*", "rds!other-*", "rds!db-**", "other-*"])
@pytest.mark.parametrize("actions", [ACTIONS, ACTIONS[1:]])
@pytest.mark.parametrize("noop", [False, True])
def test_other_prefixes_rejected(suffix, actions, noop):
    ref = ARN.replace("rds!db-*", suffix)
    raw = raw_plan(refs=[ref], actions=actions)
    if noop:
        raw["resource_changes"][2]["change"]["actions"] = ["no-op"]
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"layer": "app"},
        {"update": True},
        {"bootstrap_prepared": False},
    ],
)
def test_exception_requires_prepared_platform_bootstrap(overrides):
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize(raw_plan(), **overrides)


@pytest.mark.parametrize(
    "refs,actions",
    [
        ([ARN, ARN.replace("rds!db-*", "rds!cluster-*")], ACTIONS),
        ([ARN, ARN.replace(f.ACCOUNT, "999999999999")], ACTIONS),
        ([ARN.replace("ap-northeast-2", "us-east-1")], ACTIONS),
        ([ARN], [*ACTIONS, "secretsmanager:DeleteSecret"]),
        ([ARN], ["secretsmanager:ListSecretVersionIds"]),
    ],
)
def test_mixed_or_expanded_scope_rejected(refs, actions):
    with pytest.raises(PolicyViolation, match="ROLE_SECRET_SCOPE"):
        summarize(raw_plan(refs=refs, actions=actions))


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "ddak-flaskr-exec"),
        ("permissions_boundary", "arn:aws:iam::123456789012:policy/wrong"),
        ("path", "/ddak/infra/"),
    ],
)
def test_dbinit_address_requires_expected_role_properties(field, value):
    raw = raw_plan(unknown=True)
    raw["resource_changes"][0]["change"]["after"][field] = value
    with pytest.raises(PolicyViolation):
        summarize(raw)


def hcl(*, address=ADDRESS, ref=ARN):
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ACTIONS,
                    "Resource": ref.replace(f.ACCOUNT, "${var.account_id}"),
                }
            ]
        }
    )
    return (
        'resource "aws_iam_role_policy" "read" {\n'
        f"  role = {address}.id\n  policy = jsonencode({policy})\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "layer,address,ref,passed",
    [
        ("platform", ADDRESS, ARN, True),
        ("app", ADDRESS, ARN, False),
        ("platform", "aws_iam_role.app_execution", ARN, False),
        ("platform", ADDRESS, ARN.replace("rds!db-*", "rds!other-*"), False),
    ],
)
def test_static_gate_checks_role_reference_and_rds_prefix(layer, address, ref, passed):
    result = static_gate({"main.tf": hcl(address=address, ref=ref)}, layer=layer)
    assert result.passed is passed
    if not passed:
        assert "ROLE_SECRET_SCOPE" in result.detail


def test_runtime_cannot_use_exception_without_foundation_preparation(tmp_path):
    runner = f.FakeRunner()
    runner.raw = raw_plan(unknown=True)
    runtime = f.InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=b.SETTINGS,
        lock_file=b"fixture",
        approvals=lambda: [],
        guard=Mock(),
        runner=runner,
    )
    assert runtime.validate({"main.tf": hcl()}).passed
    with pytest.raises(DdakToolError, match="ROLE_SECRET_SCOPE"):
        runtime.plan(session=f.SESSION, analyzer=Mock(), update=False)


def test_runtime_approval_contains_dbinit_exception_and_foundation_hash(tmp_path):
    runner, sdk, records = f.FakeRunner(), b.sdk_clients(), []
    runner.raw = raw_plan(unknown=True)
    runtime = f.InfraRuntime(
        root=tmp_path,
        run_id="run-1",
        settings=b.SETTINGS,
        lock_file=b"fixture",
        approvals=lambda: records,
        guard=Mock(),
        runner=runner,
        foundation_clients=lambda session: sdk,
    )
    runtime.prepare_bootstrap(session=f.SESSION)
    assert runtime.validate({"main.tf": hcl()}).passed
    analyzer = Mock(validate_policy=Mock(return_value={"findings": []}))
    summary = runtime.plan(session=f.SESSION, analyzer=analyzer, update=False)
    encoded = encode_meta(summary, infra=True)
    row = next(
        row for row in json.loads(encoded)["iam_diff"] if row["address"] == "ddak.foundation"
    )
    assert row["bootstrap_dbinit_exception"] == {
        "layer": "platform",
        "mode": "bootstrap",
        "role_address": ADDRESS,
        "actions": ACTIONS,
        "resource": MASKED,
    }
    assert row["template_sha256"] == f.digest(
        f.canonical(foundation.foundation_template(b.SETTINGS))
    )
    assert summary["plan_sha256"] != f.digest(b"fake-saved-plan")
    assert f.ACCOUNT not in encoded
    describe = next(row for row in row["app_boundary"] if row["Action"] == ACTIONS[1])
    assert describe["Resource"] == MASKED
    assert describe["Condition"]["ArnLike"]["aws:PrincipalArn"].endswith("/ddak-*-dbinit-exec")
    templates = Path(foundation.__file__).parents[2] / "web" / "templates"
    html = (
        Environment(loader=FileSystemLoader(templates), autoescape=True)
        .get_template("approval.html")
        .render(
            approval={
                "run_id": "run-1",
                "infra_summary": json.loads(encoded),
                "subjects": {"infra": summary["plan_sha256"]},
            },
            request={"url": {"path": "/runs/run-1/approval"}},
        )
    )
    assert "rds!db-*" in html and "ap-northeast-2" in html
    assert ADDRESS in html and all(action in html for action in ACTIONS)
    assert summary["plan_sha256"] in html and f.ACCOUNT not in html
    b.assert_no_sdk_writes(sdk)


@pytest.mark.parametrize("changed_part", ["actions", "resource", "principal"])
def test_exception_change_invalidates_both_approval_hashes(tmp_path, monkeypatch, changed_part):
    old, runner, sdk, records, summary = b.runtime_plan(tmp_path / "old")
    old_template = foundation.foundation_template(b.SETTINGS)
    old_sha = f.digest(f.canonical(old_template))
    changed = deepcopy(old_template)
    if changed_part == "principal":
        describe = next(s for s in changed["boundary"]["Statement"] if s["Action"] == ACTIONS[1])
        describe["Condition"]["ArnLike"]["aws:PrincipalArn"] += "-changed"
    elif changed_part == "actions":
        changed["bootstrap_dbinit_exception"]["actions"] = ACTIONS[:1]
    else:
        changed["bootstrap_dbinit_exception"]["resource"] = ARN.replace("db-*", "db-narrow-*")
    assert old_sha != f.digest(f.canonical(changed))
    monkeypatch.setattr(foundation, "foundation_template", lambda settings: deepcopy(changed))
    with pytest.raises(DdakToolError) as exc:
        b.apply(sdk, tmp_path / "marker", [f.approval(old_sha, "foundation")])
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    records.append(f.approval(summary["plan_sha256"]))
    with pytest.raises(DdakToolError) as exc:
        old.apply(session=f.SESSION)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    new, _, new_sdk, new_records, new_summary = b.runtime_plan(tmp_path / "new")
    assert new_summary["plan_sha256"] != summary["plan_sha256"]
    new_records.append(f.approval(summary["plan_sha256"]))
    with pytest.raises(DdakToolError) as exc:
        new.apply(session=f.SESSION)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
    b.assert_no_sdk_writes(sdk)
    b.assert_no_sdk_writes(new_sdk)
    assert not any(argv[1] == "apply" for argv, _ in runner.calls)


def test_reference_hcl_and_sdk_describe_boundary_match():
    source = Path(foundation.__file__).parent / "terraform" / "foundation" / "main.tf"
    parsed = loads(source.read_text())
    app = next(
        row["aws_iam_policy"]["app_boundary"]
        for row in parsed["resource"]
        if "app_boundary" in row.get("aws_iam_policy", {})
    )
    document = json.loads(
        json.dumps(policy_json(app["policy"])).replace("${var.account_id}", f.ACCOUNT)
    )
    for candidate in (document, boundary_document(f.ACCOUNT, f.SETTINGS.project)):
        describe = [s for s in candidate["Statement"] if s["Action"] == ACTIONS[1]]
        assert describe == [
            {
                "Effect": "Allow",
                "Action": ACTIONS[1],
                "Resource": ARN,
                "Condition": {
                    "ArnLike": {
                        "aws:PrincipalArn": (
                            f"arn:aws:iam::{f.ACCOUNT}:role/ddak/app/ddak-*-dbinit-exec"
                        )
                    }
                },
            }
        ]


def test_exact_secret_masking_and_settings_are_unchanged():
    exact = ARN.replace("*", "fixture-secret")
    assert _masked(exact).startswith("RDS master ARN sha256 ")
    assert "fixture-secret" not in _masked(exact)
    assert replace(f.SETTINGS, rds_master_secret_arn=exact).rds_master_secret_arn == exact
    with pytest.raises(DdakToolError, match="정확한 ARN"):
        replace(f.SETTINGS, rds_master_secret_arn=ARN)
