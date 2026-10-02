from pathlib import Path

import hcl2
import pytest

from ddak.cloud.infra import AwsSettings
from ddak.core.contracts.infra_outputs import checked_outputs

ROOT = Path(__file__).resolve().parents[5]


def test_foundation_has_state_protection_and_two_boundaries_without_deployer():
    root = ROOT / "src/ddak/cloud/infra/terraform/foundation"
    main = hcl2.loads((root / "main.tf").read_text())
    resources = {
        kind: values
        for item in main["resource"]
        for kind, values in item.items()
        if kind != "aws_iam_policy"
    }
    policies = [item["aws_iam_policy"] for item in main["resource"] if "aws_iam_policy" in item]
    assert len(policies) == 2 and "aws_iam_role" not in resources
    assert resources["aws_s3_bucket"]["state"]["lifecycle"][0]["prevent_destroy"] is True
    block = resources["aws_s3_bucket_public_access_block"]["state"]
    assert all(
        block[key]
        for key in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        )
    )
    for name in ("variables.tf", "outputs.tf"):
        assert hcl2.loads((root / name).read_text())


def test_outputs_separate_platform_and_app_and_reject_secret_values():
    arn = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:ddak/demo/KEY-ABC123"
    assert checked_outputs({"app_secret_arn_KEY": arn}, "app")
    for values, layer in [
        ({"app_secret_arn_KEY": "value"}, "app"),
        ({"app_secret_arn_KEY": arn}, "platform"),
        ({"alb_arn": arn}, "app"),
        ({"secret_value": "value"}, "app"),
    ]:
        with pytest.raises(ValueError):
            checked_outputs(values, layer)
    AwsSettings(
        "demo",
        "123456789012",
        "fixture-state",
        "app",
        {"app_secret_arn_KEY": ("aws_secretsmanager_secret.key.arn", "string")},
    )
    with pytest.raises(Exception, match="허용 목록"):
        AwsSettings(
            "demo",
            "123456789012",
            "fixture-state",
            "app",
            {"secret_value": ("aws_secretsmanager_secret.key.arn", "string")},
        )
