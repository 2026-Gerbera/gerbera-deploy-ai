"""AWS 호출 없이 실제 Terraform/provider/Checkov로 C1 fixture를 검증한다."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hcl2.api import loads

from ddak.cloud.infra import AwsSettings, InfraRuntime
from ddak.cloud.infra.runtime import CommandRunner
from ddak.core.contracts.errors import DdakToolError


class RecordingRunner(CommandRunner):
    def __init__(self):
        super().__init__()
        self.commands = []
        self.credentials_supplied = False

    def run(self, argv, **kwargs):
        self.commands.append([Path(argv[0]).name, *argv[1:]])
        self.credentials_supplied |= kwargs.get("session") is not None
        return super().run(argv, **kwargs)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    validation = root / "harness/var/validation"
    settings = AwsSettings(
        project="flaskr",
        account_id="123456789012",
        state_bucket="ddak-fixture-state",
        layer="app",
        outputs={"secret_arn": ("aws_secretsmanager_secret.session.arn", "string")},
    )
    runner = RecordingRunner()
    source = (root / "harness/tests/fixtures/infra/app_v2.tf").read_text()
    runtime = InfraRuntime(
        root=validation / "c1-offline-runs",
        run_id="offline-c1",
        settings=settings,
        lock_file=(root / "src/ddak/cloud/infra/providers/aws.lock.hcl").read_bytes(),
        approvals=lambda: [],
        guard=lambda: None,
        timeout=120,
        runner=runner,
    )
    started = time.monotonic()
    try:
        result = runtime.validate({"main.tf": source})
        passed, detail = result.passed, result.detail
    except DdakToolError as exc:
        passed, detail = False, exc.message
    report = {
        "source": "offline-cli",
        "fixture": "app_v2.tf",
        "resources": sum(len(names) for r in loads(source)["resource"] for names in r.values()),
        "passed": passed,
        "detail": detail,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "checkov_checks": runtime.checkov_checks,
        "aws_credentials_supplied": runner.credentials_supplied,
        "commands": [
            [arg.replace(str(runtime.work), "<work>") for arg in command]
            for command in runner.commands
        ],
        "apply_requested": any("apply" in command for command in runner.commands),
    }
    (validation / "c1-offline-iam.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    runtime.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
