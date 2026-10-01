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
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.errors import DdakToolError, ErrorCode

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
    token: str

    def environment(self) -> dict[str, str]:
        if not all((self.access_key, self.secret_key, self.token)):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS 단기 세션 세 값이 필요하다")
        return {
            "AWS_ACCESS_KEY_ID": self.access_key,
            "AWS_SECRET_ACCESS_KEY": self.secret_key,
            "AWS_SESSION_TOKEN": self.token,
        }


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
            re.fullmatch(r"aws_security_group\.[A-Za-z][A-Za-z0-9_]*", a)
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
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]*", name)
                or not re.fullmatch(
                    r"aws_[a-z0-9_]+\.[A-Za-z][A-Za-z0-9_]*\.(arn|name|id|endpoint|address)",
                    expression,
                )
                or kind != "string"
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
                name: {"value": "${" + expr + "}"} for name, (expr, _) in self.outputs.items()
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
    ):
        if min(timeout, apply_timeout, refresh_timeout) <= 0:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "양수 제한 시간이 필요하다")
        root.mkdir(parents=True, exist_ok=True)
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
        self.deadline = time.monotonic() + timeout
        self._waived: set[str] = set()
        self.checkov_checks = 0
        self._files: dict[str, bytes] = {}
        self._validated = False
        self._planned: str | None = None
        self._consumed = False

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
        result = static_gate(files, layer=self.settings.layer)
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
        # fmt 검사도 원본을 수정하지 않는다. 고정된 bundle hash를 유지한다.
        if self._run("fmt", "-check", "-no-color").code != 0:
            return GateResult(False, "TERRAFORM_FMT")
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
                "-backend-config=backend.json",
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
        self._planned = plan_hash
        return summary

    def apply(self, *, session: SessionKeys) -> dict[str, Any]:
        self.deadline = time.monotonic() + self.apply_timeout
        if not self._planned or self._consumed:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "승인 가능한 새 plan이 필요하다")
        self._guard()
        self._unchanged()
        plan = self.work / "approved.tfplan"
        if plan.is_symlink() or not plan.is_file() or digest(plan.read_bytes()) != self._planned:
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
        if plan.is_symlink() or digest(plan.read_bytes()) != self._planned:
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
        result = self._run(
            "apply",
            "-input=false",
            "-no-color",
            "-lock-timeout=10s",
            "approved.tfplan",
            session=session,
        )
        if result.code != 0:
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED,
                "인프라 적용 실패; 대상 상태 확인 후 새 plan·승인이 필요하다",
            )
        plan.unlink()
        private_write(self.work / "apply-succeeded", self._planned.encode())
        output = self.refresh(session=session)
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
            return filter_outputs(
                json.loads(result.stdout), {k: v[1] for k, v in self._outputs.items()}
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
