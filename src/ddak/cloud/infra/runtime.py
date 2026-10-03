"""C1 내부 실행 API. 공유 툴 계약 확정 전에는 레지스트리에 등록하지 않는다.

Credential/SDK/승인 조회/잠금 검사는 조립 코드가 주입한다. subprocess 출력은 외부로 노출하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import IMAGE_REPOSITORY_PATTERN, checked_outputs, output_kind

from .plan import filter_outputs, summarize_plan
from .policy import GateResult, PolicyViolation, static_gate
from .providers.aws import CHECKS, REGION


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(data: Any) -> bytes:
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def private_write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)


@dataclass(frozen=True, repr=False)
class SessionKeys:
    access_key: str
    secret_key: str
    token: str | None = None

    def environment(self) -> dict[str, str]:
        if not all((self.access_key, self.secret_key)):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS 자격증명 두 값이 필요하다")
        environment = {
            "AWS_ACCESS_KEY_ID": self.access_key,
            "AWS_SECRET_ACCESS_KEY": self.secret_key,
        }
        if self.token:
            environment["AWS_SESSION_TOKEN"] = self.token
        return environment


@dataclass(frozen=True, repr=False)
class CommandResult:
    code: int
    stdout: str


class CommandRunner:
    """빈 HOME과 허용 환경만 전달한다. timeout은 프로세스 그룹을 종료하고 회수한다."""

    def __init__(self, *, stop_grace: float = 5, apply_stop_grace: float = 120):
        if min(stop_grace, apply_stop_grace) <= 0:
            raise ValueError("양수 종료 유예 시간이 필요하다")
        self.stop_grace, self.apply_stop_grace = stop_grace, apply_stop_grace

    def run(
        self, argv: Sequence[str], *, cwd: Path, deadline: float, session: SessionKeys | None = None
    ) -> CommandResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "인프라 명령 제한 시간 초과")
        with tempfile.TemporaryDirectory(prefix="ddak-tf-home-") as home:
            cli_config = Path(home) / "terraform.rc"
            cli_config.write_text("disable_checkpoint = true\n")
            env = {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": home,
                "AWS_REGION": REGION,
                "AWS_EC2_METADATA_DISABLED": "true",
                "TF_IN_AUTOMATION": "1",
                "TF_CLI_CONFIG_FILE": str(cli_config),
                "CHECKPOINT_DISABLE": "1",
            }
            if session:
                env.update(session.environment())
            try:
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen(
                        list(argv),
                        cwd=cwd,
                        env=env,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        umask=0o077,
                    )
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        # Terraform가 state를 저장하고 잠금을 풀 기회를 준다.
                        # 제한 시간 초과 결과는 여전히 불명확한 적용으로 취급한다.
                        grace = (
                            self.apply_stop_grace
                            if len(argv) > 1 and argv[1] == "apply"
                            else self.stop_grace
                        )
                        # Terraform은 두 번째 종료 신호에 즉시 종료한다.
                        # SIGINT 한 번 뒤 충분히 기다리고, 이후에만 강제 종료한다.
                        stop_deadline = time.monotonic() + grace
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGINT)
                        with suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=grace)
                        # 부모가 먼저 종료돼도 provider 자식은 같은 그룹에 남을 수 있다.
                        while True:
                            try:
                                os.killpg(process.pid, 0)
                            except ProcessLookupError:
                                break
                            left = stop_deadline - time.monotonic()
                            if left <= 0:
                                with suppress(ProcessLookupError):
                                    os.killpg(process.pid, signal.SIGKILL)
                                break
                            time.sleep(min(0.05, left))
                        process.wait()
                        raise DdakToolError(
                            ErrorCode.ADAPTER_TIMEOUT,
                            "인프라 명령 시간 초과; 대상 상태 확인이 필요하다",
                        ) from None
                    size = output.tell()
                    if size > 16 * 1024 * 1024:
                        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "인프라 명령 출력 크기 초과")
                    output.seek(0)
                    return CommandResult(
                        process.returncode, output.read().decode("utf-8", errors="replace")
                    )
            except OSError:
                raise DdakToolError(
                    ErrorCode.ADAPTER_FAILED, "인프라 명령을 실행할 수 없다"
                ) from None


def check_approval(
    records: Sequence[ApprovalRecord], *, run_id: str, project: str, kind: str, bound_to: str
) -> None:
    matched = [r for r in records if r.run_id == run_id and r.project == project and r.kind == kind]
    latest = max(matched, key=lambda r: (r.approved_at, r.decision == "denied"), default=None)
    if latest is None or latest.decision != "approved" or latest.bound_to != bound_to:
        raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "해당 인프라 산출물의 승인이 필요하다")


@dataclass(frozen=True)
class AwsSettings:
    project: str
    account_id: str
    state_bucket: str
    layer: str
    # C-03 이름 확정 전 코드 호출자만 제공한다. AI HCL은 output/variable/provider를 못 만든다.
    outputs: Mapping[str, tuple[str, str]]
    alb_security_group_addresses: tuple[str, ...] = ()
    rds_master_secret_arn: str | None = None

    def __post_init__(self) -> None:
        if self.rds_master_secret_arn is not None and (
            not self.rds_master_secret_arn.startswith(
                f"arn:aws:secretsmanager:{REGION}:{self.account_id}:secret:rds!"
            )
            or any(c in self.rds_master_secret_arn for c in ("*", "?", "${"))
        ):
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "RDS 마스터 시크릿은 정확한 ARN이 필요하다"
            )
        if not all(
            re.fullmatch(
                r"(?:aws_security_group|aws_vpc_security_group_ingress_rule)\."
                r"[A-Za-z][A-Za-z0-9_]*",
                a,
            )
            for a in self.alb_security_group_addresses
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "ALB 검사 예외 주소 형식 오류")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", self.project) or not re.fullmatch(
            r"\d{12}", self.account_id
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS 프로젝트 설정 형식 오류")
        if self.layer not in ("app", "platform") or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.state_bucket
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS state 설정 형식 오류")
        for name, (expression, kind) in self.outputs.items():
            if name == "image_repository" and self.layer == "platform":
                if kind != "string" or not re.fullmatch(
                    '"' + IMAGE_REPOSITORY_PATTERN + '"', expression
                ):
                    raise DdakToolError(ErrorCode.CONFIG_INVALID, "이미지 저장소 출력 형식 오류")
                continue
            try:
                expected_kind = output_kind(self.layer, name)
            except ValueError:
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "출력 허용 목록에 없는 이름"
                ) from None
            expressions = (
                expression[1:-1].split(",")
                if kind == "list(string)"
                and expression.startswith("[")
                and expression.endswith("]")
                else [expression]
            )
            if (
                kind != expected_kind
                or not expressions
                or any(
                    not re.fullmatch(
                        r"aws_[a-z0-9_]+\.[A-Za-z][A-Za-z0-9_]*\.(arn|name|id|endpoint|address|dns_name|master_user_secret\[0\]\.secret_arn)",
                        expr.strip(),
                    )
                    for expr in expressions
                )
            ):
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "코드 소유 출력 선언 형식 오류")

    @property
    def boundary_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/ddak/boundary/ddak-app-boundary"

    @property
    def build_boundary_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/ddak/boundary/ddak-build-boundary"

    def framework(self) -> dict[str, Any]:
        result = {
            "terraform": {
                "required_version": ">= 1.11, < 2.0",
                "required_providers": {"aws": {"source": "hashicorp/aws", "version": "~> 6.66"}},
                "backend": {"s3": {}},
            },
            "provider": {
                "aws": {
                    "region": REGION,
                    "allowed_account_ids": [self.account_id],
                    "default_tags": {"tags": {"ManagedBy": "ddak", "Project": self.project}},
                }
            },
            "variable": {
                "account_id": {"type": "string", "default": self.account_id},
                "project": {"type": "string", "default": self.project},
                "app_boundary_arn": {"type": "string", "default": self.boundary_arn},
                "build_boundary_arn": {"type": "string", "default": self.build_boundary_arn},
            },
            "output": {
                name: {
                    "value": json.loads(expr) if name == "image_repository" else "${" + expr + "}"
                }
                for name, (expr, _) in self.outputs.items()
            },
        }

        if not self.outputs:
            del result["output"]
        return result

    def backend(self) -> dict[str, Any]:
        return {
            "bucket": self.state_bucket,
            "key": f"ddak/{self.project}/{self.layer}.tfstate",
            "region": REGION,
            "encrypt": True,
            "use_lockfile": True,
            "allowed_account_ids": [self.account_id],
        }


class InfraRuntime:
    """한 run/layer의 validate→plan→승인→apply.

    root는 재시작/다중 호출에 공유하는 영속 디렉토리여야 한다.
    apply-attempt 표식은 임시 파일 정리 대상이 아니다. 자동 재실행은 금지한다.
    """

    def __init__(
        self,
        *,
        root: Path,
        run_id: str,
        settings: AwsSettings,
        lock_file: bytes,
        approvals: Callable[[], Sequence[ApprovalRecord]],
        guard: Callable[[], None],
        runner: CommandRunner | None = None,
        terraform: str = "terraform",
        checkov: str = "checkov",
        timeout: float = 120,
        apply_timeout: float = 1200,
        refresh_timeout: float = 30,
        migration_timeout: float = 120,
        foundation_clients: Callable[[SessionKeys], Mapping[str, Any]] | None = None,
    ):
        if min(timeout, apply_timeout, refresh_timeout, migration_timeout) <= 0:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "양수 제한 시간이 필요하다")
        root.mkdir(parents=True, exist_ok=True)
        bootstrap_identity = digest(canonical([settings.project, settings.layer])).split(":")[1]
        self._bootstrap_attempt = root / f"bootstrap-recovery-{bootstrap_identity}.json"
        if self._bootstrap_attempt.exists():
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                f"이전 로컬 state 복구 확인 필요: {self._bootstrap_attempt}",
                needs_human=True,
            )
        self.work = Path(tempfile.mkdtemp(prefix="infra-", dir=root))
        identity = digest(canonical([settings.project, run_id, settings.layer])).split(":")[1]
        self._attempt = root / f"apply-attempt-{identity}"
        self.run_id = run_id
        self.settings = settings
        self._outputs = dict(settings.outputs)
        self._framework = canonical(settings.framework())
        self._backend = canonical(settings.backend())
        self._lock_file = lock_file
        self._approval_reader = approvals
        self._guard = guard
        self.runner = runner or CommandRunner()
        self.terraform = shutil.which(terraform) or terraform
        self.checkov = shutil.which(checkov) or checkov
        self.timeout = timeout
        self.apply_timeout, self.refresh_timeout = apply_timeout, refresh_timeout
        self.migration_timeout = migration_timeout
        self.deadline = time.monotonic() + timeout
        self._waived: set[str] = set()
        self.checkov_checks = 0
        self._files: dict[str, bytes] = {}
        self._validated = False
        self._planned: str | None = None
        self._consumed = False
        self.foundation_clients = foundation_clients
        self._foundation: bytes | None = None
        self._local_backend = False

    def _clients(self, session: SessionKeys) -> Mapping[str, Any]:
        if self.foundation_clients is not None:
            return self.foundation_clients(session)
        session.environment()  # 빈 세션이나 기본 credential chain 사용을 허용하지 않는다.
        sdk = boto3.Session(
            aws_access_key_id=session.access_key,
            aws_secret_access_key=session.secret_key,
            aws_session_token=session.token or None,
            region_name=REGION,
        )
        config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
        return {name: sdk.client(name, config=config) for name in ("s3", "iam", "sts")}

    def prepare_bootstrap(self, *, session: SessionKeys) -> None:
        """플랫폼 첫 실행의 기반 확보도 같은 infra 승인에 묶는다. 여기서는 조회만 한다."""
        from .foundation import foundation_template

        if self._files or self.settings.layer != "platform":
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "새 플랫폼 검증 전에 기반을 계획해야 한다"
            )
        clients = self._clients(session)
        try:
            if clients["sts"].get_caller_identity().get("Account") != self.settings.account_id:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "기반 계획 세션의 대상 계정이 다르다"
                )
            try:
                clients["s3"].head_bucket(
                    Bucket=self.settings.state_bucket, ExpectedBucketOwner=self.settings.account_id
                )
                self._local_backend = False
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
                    raise
                self._local_backend = True
        except DdakToolError:
            raise
        except Exception:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "기반 버킷 존재 확인 실패") from None
        self._foundation = canonical(foundation_template(self.settings))
        framework = self.settings.framework()
        if self._local_backend:
            framework["terraform"]["backend"] = {"local": {}}
        self._framework = canonical(framework)

    def _approval_hash(self, plan_hash: str) -> str:
        if self._foundation is None:
            return plan_hash
        from .foundation import foundation_template

        if canonical(foundation_template(self.settings)) != self._foundation:
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인할 기반 템플릿이 바뀌었다")
        return digest(
            canonical(
                {
                    "plan": plan_hash,
                    "foundation": digest(self._foundation),
                    "backend": self.settings.backend(),
                    "local_backend": self._local_backend,
                }
            )
        )

    def _migrate_backend(self, session: SessionKeys) -> None:
        self.deadline = time.monotonic() + self.migration_timeout
        # 원문 state를 읽지 않는다. 새 버킷에도 대상 state가 생겼다면 덮어쓰지 않는다.
        client = self._clients(session)["s3"]
        lock_key = self.settings.backend()["key"] + ".tflock"
        lock_info = {
            "ID": str(uuid.uuid4()),
            "Operation": "OperationTypeMigrateState",
            "Info": self.run_id,
            "Who": "ddak",
            "Version": "1.11.0",
            "Created": datetime.now(UTC).isoformat(),
            "Path": self.settings.state_bucket + "/" + self.settings.backend()["key"],
        }
        # Terraform과 같은 S3 lock key를 먼저 확보한 뒤 대상 부재를 검사한다.
        # 상태 불명 실패에서는 이 lock도 보존한다. 정상 완료에서만 자기 ETag로 해제한다.
        lock_result = client.put_object(
            Bucket=self.settings.state_bucket,
            Key=lock_key,
            ExpectedBucketOwner=self.settings.account_id,
            IfNoneMatch="*",
            Body=canonical(lock_info),
            ContentType="application/json",
        )
        etag = lock_result.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "state 이전 잠금 소유권을 확인할 수 없다")
        try:
            client.head_object(
                Bucket=self.settings.state_bucket,
                Key=self.settings.backend()["key"],
                ExpectedBucketOwner=self.settings.account_id,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
        else:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "원격 state가 이미 있다; 이전 중단")
        self._guard()
        self._unchanged()
        framework = canonical(self.settings.framework())
        (self.work / "ddak.tf.json").write_bytes(framework)
        self._files["ddak.tf.json"] = framework
        if self._run(
            "init",
            "-input=false",
            "-lockfile=readonly",
            "-migrate-state",
            "-force-copy",
            "-lock=false",  # 이 구간은 위에서 확보한 동일 S3 잠금이 보호한다.
            "-lock-timeout=10s",
            "-backend-config=backend.json",
            "-no-color",
            session=session,
        ).code:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "원격 backend 이전 실패")
        client.delete_object(
            Bucket=self.settings.state_bucket,
            Key=lock_key,
            ExpectedBucketOwner=self.settings.account_id,
            IfMatch=etag,
        )
        private_write(self.work / "backend-migrated", self._planned.encode())

    def _run(
        self, *args: str, session: SessionKeys | None = None, checkov: bool = False
    ) -> CommandResult:
        return self.runner.run(
            [self.checkov if checkov else self.terraform, *args],
            cwd=self.work,
            deadline=self.deadline,
            session=session,
        )

    def _unchanged(self) -> None:
        for name, expected in self._files.items():
            path = self.work / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검증된 인프라 파일이 바뀌었다")
        actual = {
            p.name
            for p in self.work.iterdir()
            if p.name.endswith((".tf", ".tf.json", ".tfvars", ".tfvars.json"))
        }
        expected_names = {
            n for n in self._files if n.endswith((".tf", ".tf.json", ".tfvars", ".tfvars.json"))
        }
        if actual != expected_names:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인되지 않은 인프라 파일이 있다")

    def _checkov(self, *, plan: Path | None = None) -> dict[str, Any]:
        args = ["--file", str(plan)] if plan else ["--directory", str(self.work)]
        result = self._run(
            *args,
            "--framework",
            "terraform_plan" if plan else "terraform",
            "--check",
            ",".join(CHECKS),
            "--skip-download",
            "--output",
            "json",
            checkov=True,
        )
        try:
            data = json.loads(result.stdout)
            if isinstance(data, list):
                data = next(
                    d for d in data if d.get("check_type") in ("terraform", "terraform_plan")
                )
            # 선택한 검사 중 이 리소스에 적용되는 항목이 없으면 Checkov는 summary만 낸다.
            if "results" not in data:
                expected = ("passed", "failed", "skipped", "parsing_errors", "resource_count")
                if result.code == 0 and all(
                    type(data.get(k)) is int and data[k] == 0 for k in expected
                ):
                    return {"passed": True, "failed": []}
                raise ValueError
            checks = data["results"]
            failed = checks["failed_checks"]
            skipped = checks["skipped_checks"]
            self.checkov_checks += len(checks["passed_checks"]) + len(failed) + len(skipped)
            parsing = checks.get("parsing_errors", [])
            if result.code not in (0, 1) or skipped or parsing:
                raise ValueError
            waived = [
                c
                for c in failed
                if c["check_id"] == "CKV_AWS_260"
                and c.get("resource") in self.settings.alb_security_group_addresses
            ]
            self._waived.update(c["resource"] for c in waived)
            failed = [c for c in failed if c not in waived]
            ids = sorted({c["check_id"] for c in failed})
            if not all(re.fullmatch(r"CKV_[A-Z0-9_]+", x) for x in ids):
                raise ValueError
            return {"passed": not failed and (result.code == 0 or bool(waived)), "failed": ids}
        except (ValueError, TypeError, KeyError, StopIteration):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Checkov 결과를 확인할 수 없다") from None

    def validate(self, files: Mapping[str, str]) -> GateResult:
        self.deadline = time.monotonic() + self.timeout
        if self._files:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "새 검증은 새 인프라 실행에서 시작한다"
            )
        result = static_gate(
            files, layer=self.settings.layer, state_bucket=self.settings.state_bucket
        )
        if not result.passed:
            return result
        self._files = {name: source.encode() for name, source in files.items()}
        self._files.update(
            {
                "ddak.tf.json": self._framework,
                ".terraform.lock.hcl": self._lock_file,
                "backend.json": self._backend,
            }
        )
        for name, content in self._files.items():
            private_write(self.work / name, content)
        # 서식은 실행 안전성과 무관하다. AI 생성 번들의 내용을 자동 변경하지 않고
        # init/validate/Checkov로 구문·공급자 계약·보안 정책을 검사한다.
        if (
            self._run(
                "init", "-input=false", "-lockfile=readonly", "-backend=false", "-no-color"
            ).code
            != 0
        ):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Terraform provider 초기화 실패")
        result = self._run("validate", "-json")
        try:
            valid = json.loads(result.stdout)["valid"] is True
        except (ValueError, KeyError, TypeError):
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, "Terraform validate 결과 형식 오류"
            ) from None
        if result.code != 0 or not valid:
            return GateResult(False, "TERRAFORM_VALIDATE")
        if not self._checkov()["passed"]:
            return GateResult(False, "CHECKOV_FAILED")
        self._unchanged()
        self._validated = True
        return GateResult(True)

    def plan(self, *, session: SessionKeys, analyzer: Any, update: bool = True) -> dict[str, Any]:
        self.deadline = time.monotonic() + self.timeout
        if not self._validated or self._consumed or self._planned:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "검증한 새 인프라 실행이 필요하다")
        self._guard()
        self._unchanged()
        if (
            self._run(
                "init",
                "-input=false",
                "-lockfile=readonly",
                "-reconfigure",
                *([] if self._local_backend else ["-backend-config=backend.json"]),
                "-no-color",
                session=session,
            ).code
            != 0
        ):
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Terraform backend 초기화 실패")
        plan = self.work / "approved.tfplan"
        result = self._run(
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=10s",
            "-out=approved.tfplan",
            "-detailed-exitcode",
            session=session,
        )
        if result.code not in (0, 2):
            plan.unlink(missing_ok=True)
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Terraform plan 실패; 다시 계획해야 한다")
        plan.chmod(0o600)
        plan_hash = digest(plan.read_bytes())
        json_plan = self.work / "checkov-plan.json"
        try:
            shown = self._run("show", "-json", "approved.tfplan", session=session)
            if shown.code != 0:
                raise ValueError
            raw = json.loads(shown.stdout)
            private_write(json_plan, shown.stdout.encode())
            checked = self._checkov(plan=json_plan)
            summary = summarize_plan(
                raw,
                layer=self.settings.layer,
                plan_sha256=plan_hash,
                exit_code=result.code,
                account_id=self.settings.account_id,
                boundary_arn=self.settings.boundary_arn,
                build_boundary_arn=self.settings.build_boundary_arn,
                project=self.settings.project,
                rds_master_secret_arn=self.settings.rds_master_secret_arn,
                state_bucket=self.settings.state_bucket,
                update=update,
                analyzer=analyzer,
                checkov=checked,
            )
            if self._waived:
                summary["headline"] += " · 코드 지정 ALB 공개 HTTP 예외: " + ", ".join(
                    sorted(self._waived)
                )
                if len(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()) > 8192:
                    raise PolicyViolation("SUMMARY_TOO_LARGE")
            self._unchanged()
            if digest(plan.read_bytes()) != plan_hash:
                raise ValueError
        except PolicyViolation as exc:
            plan.unlink(missing_ok=True)
            raise DdakToolError(ErrorCode.CONFIG_INVALID, str(exc)) from None
        except DdakToolError:
            plan.unlink(missing_ok=True)
            raise
        except Exception:
            plan.unlink(missing_ok=True)
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "인프라 plan 검사 수행 실패") from None
        finally:
            json_plan.unlink(missing_ok=True)
        self._planned = self._approval_hash(plan_hash)
        if self._foundation is not None:
            from .plan import _policy_view

            foundation = json.loads(self._foundation)
            summary["plan_sha256"] = self._planned
            summary["headline"] += " · 기반 버킷·권한 경계 2개 확보 및 원격 state 연결"
            summary["iam_diff"].append(
                {
                    "address": "ddak.foundation",
                    "action": "ensure",
                    "template_sha256": digest(self._foundation),
                    "bucket": foundation["bucket"].replace(
                        self.settings.account_id, "************"
                    ),
                    "app_boundary": _policy_view(foundation["boundary"]),
                    "build_boundary": _policy_view(foundation["build_boundary"]),
                    "state_migration": self._local_backend,
                }
            )
            if len(canonical(summary)) > 8192:
                self._planned = None
                raise DdakToolError(ErrorCode.CONFIG_INVALID, "기반 포함 승인 요약 크기 초과")
        return summary

    def apply(self, *, session: SessionKeys) -> dict[str, Any]:
        self.deadline = time.monotonic() + self.apply_timeout
        if not self._planned or self._consumed:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 가능한 새 plan이 필요하다")
        self._guard()
        self._unchanged()
        plan = self.work / "approved.tfplan"
        if (
            plan.is_symlink()
            or not plan.is_file()
            or self._approval_hash(digest(plan.read_bytes())) != self._planned
        ):
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인할 plan 파일이 바뀌었다")
        check_approval(
            self._approval_reader(),
            run_id=self.run_id,
            project=self.settings.project,
            kind="infra",
            bound_to=self._planned,
        )
        # 승인 조회 사이에 파일이나 잠금이 바뀌었는지도 다시 검사한다.
        self._guard()
        self._unchanged()
        if plan.is_symlink() or self._approval_hash(digest(plan.read_bytes())) != self._planned:
            raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "승인 조회 중 plan 파일이 바뀌었다")
        # 프로세스 시작 직전에 기록한다. 중단/실패 후 같은 run을 자동 재실행할 수 없다.
        marker = self.work / "apply-started"
        if marker.exists() or self._attempt.exists():
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 apply의 상태를 확인해야 한다")
        if time.monotonic() >= self.deadline:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "인프라 실행 전 제한 시간이 초과됐다")
        try:
            private_write(self._attempt, self._planned.encode())
        except FileExistsError:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "같은 run의 apply 시도가 이미 있다"
            ) from None
        private_write(marker, self._planned.encode())
        self._consumed = True
        started = time.monotonic()
        try:
            if self._local_backend:
                private_write(
                    self._bootstrap_attempt,
                    canonical(
                        {
                            "run_id": self.run_id,
                            "project": self.settings.project,
                            "layer": self.settings.layer,
                            "plan_sha256": self._planned,
                            "local_state_path": str(self.work / "terraform.tfstate"),
                            "work_dir": str(self.work),
                        }
                    ),
                )
            if self._foundation is not None:
                from .foundation import apply_foundation

                clients = self._clients(session)
                apply_foundation(
                    settings=self.settings,
                    run_id=self.run_id,
                    s3=clients["s3"],
                    iam=clients["iam"],
                    sts=clients["sts"],
                    approvals=self._approval_reader,
                    guard=self._guard,
                    marker=self._attempt.with_name(self._attempt.name + "-foundation"),
                    infra_subject=self._planned,
                    expected_bucket_exists=not self._local_backend,
                )
                self._guard()
                self._unchanged()
                check_approval(
                    self._approval_reader(),
                    run_id=self.run_id,
                    project=self.settings.project,
                    kind="infra",
                    bound_to=self._planned,
                )
                if (
                    plan.is_symlink()
                    or self._approval_hash(digest(plan.read_bytes())) != self._planned
                ):
                    raise DdakToolError(ErrorCode.APPROVAL_REQUIRED, "기반 생성 중 plan이 바뀌었다")
            result = self._run(
                "apply",
                "-input=false",
                "-no-color",
                "-lock-timeout=10s",
                "approved.tfplan",
                session=session,
            )
            if result.code != 0:
                raise DdakToolError(ErrorCode.ADAPTER_FAILED, "인프라 적용 실패")
            if self._local_backend:
                self._migrate_backend(session)
                self._bootstrap_attempt.unlink()
            plan.unlink()
            output = self.refresh(session=session)
            private_write(self.work / "apply-succeeded", self._planned.encode())
        except Exception as exc:
            # Terraform은 일부 리소스만 변경하고 실패할 수 있다. 앱 컨테이너
            # 롤백으로 복구됐다고 판단하지 않고 잠금과 증거를 보존한다.
            code = exc.code if isinstance(exc, DdakToolError) else ErrorCode.ADAPTER_FAILED
            cause = str(exc) if isinstance(exc, DdakToolError) else type(exc).__name__
            raise DdakToolError(
                code,
                "인프라 적용 또는 출력 확인 실패; "
                + cause
                + "; 대상 상태를 사람이 확인해야 한다"
                + (
                    f"; 복구 기록: {self._bootstrap_attempt}"
                    if self._bootstrap_attempt.exists()
                    else ""
                ),
                needs_human=True,
            ) from None
        return {
            "outputs": output,
            "elapsed_seconds": time.monotonic() - started,
            "plan_sha256": self._planned,
        }

    def refresh(self, *, session: SessionKeys) -> dict[str, Any]:
        self.deadline = time.monotonic() + self.refresh_timeout
        result = self._run("output", "-json", session=session)
        try:
            if result.code != 0:
                raise ValueError
            return checked_outputs(
                filter_outputs(
                    json.loads(result.stdout), {k: v[1] for k, v in self._outputs.items()}
                ),
                self.settings.layer,
            )
        except (ValueError, KeyError, TypeError):
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED,
                "인프라 출력 갱신 실패; 민감 출력 또는 출력 계약을 확인해야 한다",
            ) from None

    def close(self) -> None:
        """검증/plan 또는 성공한 실행의 사본 정리. 미확인 apply 상태는 사람이 확인한다."""
        if (self.work / "apply-started").exists() and not (self.work / "apply-succeeded").exists():
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "불명확한 apply 산출물은 보존한다")
        shutil.rmtree(self.work, ignore_errors=False)
