"""생성 번들의 바이트/해시를 확정하고 코드 소유 C1 실행 세션에 연결한다."""

from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path

import boto3
from botocore.config import Config

from ddak.core.aws_credentials import aws_settings, checked_session
from ddak.core.config import AdapterMode
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraOutput
from ddak.core.project_settings import cloud_platform_name
from ddak.core.snapshots import digest_bytes

from .bindings import InfraBinding
from .providers.aws import REGION
from .runtime import AwsSettings, InfraRuntime, SessionKeys


def read_bundle(bundle: GenerateInfraOutput, directory: Path) -> dict[str, str]:
    """다른 경로·symlink·추가 파일을 거부하고 확인한 바이트만 runtime에 넘긴다."""
    if directory.is_symlink() or Path(bundle.directory) != directory:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들의 디렉터리가 다르다")
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != set(bundle.files):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들의 파일 목록이 다르다")
    files = {}
    total = 0
    for name, expected in bundle.files.items():
        path = directory / name
        if Path(name).name != name or not name.endswith(".tf") or path.is_symlink():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들에는 직접 하위 .tf만 허용한다")
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들 파일 크기/형식 오류")
        data = path.read_bytes()
        total += len(data)
        if total > 4 * 1024 * 1024 or digest_bytes(data) != expected:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "생성 번들 해시/크기 불일치")
        try:
            files[name] = data.decode("utf-8")
        except UnicodeError:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 번들은 UTF-8이어야 한다") from None
    return files


def create_binding(
    bundle: GenerateInfraOutput,
    files: dict[str, str],
    ctx: RunContext,
    *,
    root: Path,
    approvals: Callable[[], Sequence[ApprovalRecord]],
    guard: Callable[[], None],
) -> InfraBinding:
    """실제 제품의 SDK 연결. FAKE는 시험이 제공하는 factory만 쓴다."""
    if ctx.adapter_mode is AdapterMode.FAKE:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "FAKE 인프라에는 fixture binding factory가 필요하다"
        )
    config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
    selection = aws_settings(ctx.project_settings)

    def session_keys() -> SessionKeys:
        return SessionKeys.from_sdk(sdk_session(), profile_name=selection["aws_profile"])

    def sdk_session():
        return checked_session(
            selection,
            region_name=REGION,
            config=config,
            session_factory=boto3.Session,
        )

    sdk_session()
    account = selection["aws_expected_account_id"]
    # state·리소스는 클라우드 플랫폼 이름, 승인 기록은 프로젝트 이름으로 찾는다.
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    settings = AwsSettings(
        project=platform,
        approval_project=ctx.project,
        account_id=account,
        state_bucket=f"ddak-state-{account}-{sha256(platform.encode()).hexdigest()[:16]}",
        layer=bundle.layer,
        outputs=bundle.outputs,
        alb_security_group_addresses=(
            "aws_security_group.alb",
            "aws_vpc_security_group_ingress_rule.alb_http",
        )
        if bundle.layer == "platform"
        else (),
        rds_master_secret_arn=ctx.platform.get("cloud", {}).get("rds_master_secret_arn"),
        storage_intent=(ctx.project_settings.get("_infra_storage") or {}).get("intent"),
        task_role_arn=ctx.platform.get("cloud", {}).get("task_role_arn"),
    )
    runtime = InfraRuntime(
        root=root,
        run_id=ctx.run_id,
        settings=settings,
        lock_file=(Path(__file__).parent / "providers/aws.lock.hcl").read_bytes(),
        approvals=approvals,
        guard=guard,
        aws_project_settings=selection,
    )
    return InfraBinding(
        runtime,
        files,
        ctx.adapter_mode,
        session_keys,
        session_keys,
        lambda: sdk_session().client("accessanalyzer", config=config),
        baseline_root=root.parent,
    )
