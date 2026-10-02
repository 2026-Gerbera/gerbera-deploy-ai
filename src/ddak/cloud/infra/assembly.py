"""생성 번들의 바이트/해시를 확정하고 코드 소유 C1 실행 세션에 연결한다."""

from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path

import boto3
from botocore.config import Config

from ddak.core.config import AdapterMode
from ddak.core.contracts.approval import ApprovalRecord
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraOutput
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

    def session_keys() -> SessionKeys:
        try:
            sdk = boto3.Session(region_name=REGION)
            credentials = sdk.get_credentials()
            if credentials is None:
                raise ValueError
            credentials = credentials.get_frozen_credentials()
            if credentials.token:
                return SessionKeys(
                    credentials.access_key, credentials.secret_key, credentials.token
                )
            temporary = sdk.client("sts", config=config).get_session_token(DurationSeconds=3600)[
                "Credentials"
            ]
            return SessionKeys(
                temporary["AccessKeyId"], temporary["SecretAccessKey"], temporary["SessionToken"]
            )
        except Exception:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "AWS 단기 자격증명을 확보할 수 없다"
            ) from None

    def sdk_session():
        keys = session_keys()
        return boto3.Session(
            aws_access_key_id=keys.access_key,
            aws_secret_access_key=keys.secret_key,
            aws_session_token=keys.token,
            region_name=REGION,
        )

    try:
        account = sdk_session().client("sts", config=config).get_caller_identity()["Account"]
    except Exception:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "인프라 대상 AWS 계정을 확인할 수 없다"
        ) from None
    settings = AwsSettings(
        project=ctx.project,
        account_id=account,
        state_bucket=f"ddak-state-{account}-{sha256(ctx.project.encode()).hexdigest()[:16]}",
        layer=bundle.layer,
        outputs=bundle.outputs,
        rds_master_secret_arn=ctx.platform.get("cloud", {}).get("rds_master_secret_arn"),
    )
    runtime = InfraRuntime(
        root=root,
        run_id=ctx.run_id,
        settings=settings,
        lock_file=(Path(__file__).parent / "providers/aws.lock.hcl").read_bytes(),
        approvals=approvals,
        guard=guard,
    )
    return InfraBinding(
        runtime,
        files,
        ctx.adapter_mode,
        session_keys,
        session_keys,
        lambda: sdk_session().client("accessanalyzer", config=config),
    )
