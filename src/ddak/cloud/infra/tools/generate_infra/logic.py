"""AI가 최초 HCL 전체를 제안하고, 검증 실패 뒤에는 기존 파일 일부만 교정한다."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

from pydantic import Field, field_validator

from ddak.core.ai.gateway import call_ai
from ddak.core.config import Settings
from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.project_settings import cloud_platform_name
from ddak.core.redact import MAX_LEN, redact
from ddak.core.snapshots import digest_bytes

PROMPT_VERSION = "infra-aws-v3-rolling"
_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
_REPAIR_PROMPT = (Path(__file__).parent / "repair_prompt.md").read_text(encoding="utf-8")
_REPAIR_INSTRUCTION = (
    _REPAIR_PROMPT
    + "\n\nThe following original generation requirements still apply. In repair mode, the "
    "partial-file rules above override any request below to regenerate the complete bundle.\n\n"
    + _PROMPT
)
_FILE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\.tf$")
_NAMESPACE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
# 이미지 저장소 앞부분을 쓸 때는 레지스트리 주소(점 포함)를 Docker Hub 네임스페이스로 보지 않는다.
_HUB_NAMESPACE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
_FEEDBACK = re.compile(r"^[A-Z][A-Z0-9_]{0,63}(?:,[A-Z][A-Z0-9_]{0,63}){0,2}$")
_CODEBUILD_BUILDSPEC = "version: 0.2\\nphases:\\n  build:\\n    commands:\\n      - exit 1\\n"
_DOUBLE_ESCAPED_CODEBUILD_BUILDSPEC = _CODEBUILD_BUILDSPEC.replace("\\", "\\\\")
_MAX_BUNDLE = 1024 * 1024


class TerraformFileDraft(ContractModel):
    name: str
    lines: tuple[str, ...] = Field(min_length=1)

    @field_validator("lines")
    @classmethod
    def one_hcl_line_per_item(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\n" in line or "\r" in line for line in value):
            raise ValueError("각 Terraform 줄은 배열 항목 하나여야 한다")
        return value


class TerraformDraft(ContractModel):
    files: tuple[TerraformFileDraft, ...] = Field(min_length=1, max_length=16)


def _normalize_code_owned_literals(source: str) -> str:
    expected = f'buildspec = "{_CODEBUILD_BUILDSPEC}"'
    doubled = f'buildspec = "{_DOUBLE_ESCAPED_CODEBUILD_BUILDSPEC}"'
    return source.replace(doubled, expected)


def _platform_outputs(namespace: str, project: str) -> dict[str, tuple[str, str]]:
    return {
        "vpc_id": ("aws_vpc.main.id", "string"),
        "cluster_arn": ("aws_ecs_cluster.main.arn", "string"),
        "cluster_name": ("aws_ecs_cluster.main.name", "string"),
        "alb_arn": ("aws_lb.main.arn", "string"),
        "alb_dns_name": ("aws_lb.main.dns_name", "string"),
        "alb_security_group_id": ("aws_security_group.alb.id", "string"),
        "certificate_arn": ("aws_acm_certificate.main.arn", "string"),
        "https_listener_arn": ("aws_lb_listener.https.arn", "string"),
        "http_listener_arn": ("aws_lb_listener.http.arn", "string"),
        "rds_endpoint": ("aws_db_instance.main.endpoint", "string"),
        "rds_master_secret_arn": (
            "aws_db_instance.main.master_user_secret[0].secret_arn",
            "string",
        ),
        "codebuild_project_name": ("aws_codebuild_project.main.name", "string"),
        "image_repository": (f'"{namespace}/{project}"', "string"),
        "source_bucket": ("aws_s3_bucket.source.id", "string"),
        "dockerhub_push_secret_arn": ("aws_secretsmanager_secret.dockerhub_push.arn", "string"),
        "dockerhub_pull_secret_arn": ("aws_secretsmanager_secret.dockerhub_pull.arn", "string"),
        "private_subnet_ids": (
            "[aws_subnet.private_a.id,aws_subnet.private_b.id]",
            "list(string)",
        ),
        "public_subnet_ids": (
            "[aws_subnet.public_a.id,aws_subnet.public_b.id]",
            "list(string)",
        ),
        "ecs_service_name": ("aws_ecs_service.app.name", "string"),
        "app_security_group_id": ("aws_security_group.app.id", "string"),
        "target_group_arn": ("aws_lb_target_group.app.arn", "string"),
        "task_execution_role_arn": ("aws_iam_role.task_execution.arn", "string"),
        "task_role_arn": ("aws_iam_role.task.arn", "string"),
        "dbinit_execution_role_arn": ("aws_iam_role.dbinit_execution.arn", "string"),
        "app_secret_arn_SECRET_KEY": (
            "aws_secretsmanager_secret.app_secret_key.arn",
            "string",
        ),
        "app_secret_arn_DATABASE_URL": (
            "aws_secretsmanager_secret.app_database_url.arn",
            "string",
        ),
    }


def _dockerhub_namespace(ctx: RunContext) -> str:
    """DDAK_DOCKERHUB_NAMESPACE가 우선이고, 없으면 프로젝트 이미지 저장소 설정의 앞부분을 쓴다."""
    namespace = os.environ.get("DDAK_DOCKERHUB_NAMESPACE", "").strip()
    if not namespace:
        repository = ctx.project_settings.get("image_repository") or ctx.image_repository
        head = repository.split("/", 1)[0] if isinstance(repository, str) else ""
        namespace = head if _HUB_NAMESPACE.fullmatch(head) else ""
    if not _NAMESPACE.fullmatch(namespace):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "Docker Hub 네임스페이스가 필요하다. 관리 페이지 '초기 연결 설정'의 이미지 저장소"
            "(예: 2026gerbera/flaskr)를 입력하거나 DDAK_DOCKERHUB_NAMESPACE를 설정한다",
        )
    return namespace


def _safe_data(ctx: RunContext, namespace: str) -> str:
    settings = ctx.project_settings
    domain = settings.get("cloud_domain") or ctx.cloud_domain
    zone = settings.get("hosted_zone_id")
    if settings.get("dns_mode") != "route53" or not domain or not zone:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "최초 실배포는 자동 ACM 검증을 위해 Route 53 도메인과 hosted zone ID가 필요하다",
            needs_human=True,
        )
    if not ctx.repo_url:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "GitHub 저장소 URL이 필요하다")
    return (
        f"project={cloud_platform_name(ctx.project, ctx.project_settings)}\n"
        f"domain={domain}\n"
        f"hosted_zone_id={zone}\n"
        f"repository_url={ctx.repo_url.removesuffix('.git')}\n"
        f"dockerhub_namespace={namespace}\n"
        "region=ap-northeast-2\n"
        "availability_zones=ap-northeast-2a,ap-northeast-2c"
    )


def _repair_source(ctx: RunContext, directory: Path) -> tuple[str, dict[str, str]] | None:
    feedback = ctx.project_settings.get("_infra_validation_feedback")
    previous = ctx.project_settings.get("_infra_repair_directory")
    if feedback is None and previous is None:
        return None
    if not isinstance(feedback, str) or not _FEEDBACK.fullmatch(feedback):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 교정 오류 코드 형식 오류")
    if not isinstance(previous, str):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 교정 원본이 없다")
    original = Path(previous)
    root = original.resolve()
    if (
        root.parent != directory.parent
        or not root.name.startswith(ctx.run_id)
        or not root.is_dir()
        or original.is_symlink()
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 교정 원본 경로 오류")
    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or not _FILE.fullmatch(path.name):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 교정 원본 파일 형식 오류")
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) > 256 * 1024 or total > _MAX_BUNDLE:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 교정 원본 크기 초과")
        try:
            files[path.name] = raw.decode("utf-8")
        except UnicodeError:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "인프라 교정 원본은 UTF-8이어야 한다"
            ) from None
    if len(files) < 2:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "부분 교정에는 둘 이상의 원본 파일이 필요하다"
        )
    return feedback, files


def _baseline_source(platform: str, directory: Path) -> dict[str, str] | None:
    root = directory.parent.parent / "infra-baselines" / platform / PROMPT_VERSION
    if not root.exists():
        return None
    if not root.is_dir() or root.is_symlink():
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 기준본 경로 오류")
    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or not _FILE.fullmatch(path.name):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 기준본 파일 형식 오류")
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) > 256 * 1024 or total > _MAX_BUNDLE:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 기준본 크기 초과")
        try:
            files[path.name] = raw.decode("utf-8")
        except UnicodeError:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "인프라 기준본은 UTF-8이어야 한다"
            ) from None
    if len(files) < 2:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 기준본 파일이 부족하다")
    return files


def _repair_data(base: str, feedback: str, files: dict[str, str]) -> str:
    parts = [base, f"validation_error={feedback}", "existing_files_begin"]
    for name, source in files.items():
        parts.extend((f"file={name}", source, "file_end"))
    parts.append("existing_files_end")
    return "\n".join(parts)


def _bounded_ai_data(data: str) -> str:
    # gateway와 같은 정제를 하되, 길이 초과를 자르기 전에 확인한다.
    safe = redact(data, max_len=None)
    if len(safe) > MAX_LEN:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            f"NEEDS_CONTEXT: generate_infra AI 입력이 정제 후 gateway의 {MAX_LEN}자 "
            "한도를 초과한다. 입력을 자르지 않고 거부하며, 전체 교정 문맥 전달 방안이 필요하다",
            needs_human=True,
        )
    return safe


def _draft_files(result: TerraformDraft) -> dict[str, str]:
    files: dict[str, str] = {}
    for draft in result.files:
        if not _FILE.fullmatch(draft.name) or draft.name in files:
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "Terraform 파일 이름 오류")
        files[draft.name] = _normalize_code_owned_literals("\n".join(draft.lines) + "\n")
    return files


def _write_bundle(directory: Path, rendered: dict[str, str]) -> dict[str, str]:
    digests: dict[str, str] = {}
    total = 0
    for name, source in rendered.items():
        if not _FILE.fullmatch(name) or not source.strip():
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "Terraform 파일 이름/내용 오류")
        raw = source.encode("utf-8")
        total += len(raw)
        if len(raw) > 256 * 1024 or total > _MAX_BUNDLE:
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "Terraform 생성 결과 크기 초과")
        try:
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        except OSError as exc:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Terraform 번들을 쓸 수 없다") from exc
        digests[name] = digest_bytes(raw)
    return digests


def generate_infra(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
    if inp.run_id != ctx.run_id:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 생성 run ID가 다르다")
    if inp.layer == "app":
        if ctx.mode != RunMode.UPDATE:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID,
                "앱 인프라 갱신에는 새 시크릿 키 목록 계약이 필요하다; 저장소는 UPDATE만 허용한다",
                needs_human=True,
            )
        from .storage import generate_storage

        return generate_storage(inp, ctx, ai=call_ai)
    if inp.layer != "platform":
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "앱 인프라 갱신에는 새 시크릿 키 목록 계약이 필요하다",
            needs_human=True,
        )
    namespace = _dockerhub_namespace(ctx)
    # 기존 클라우드 플랫폼을 재사용할 수 있게 생성 입력·기준본·출력은 플랫폼 이름을 쓴다.
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    original = Path(inp.directory)
    directory = original.resolve()
    if not directory.is_dir() or original.is_symlink() or any(directory.iterdir()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "비어 있는 인프라 번들 디렉터리가 필요하다")

    base = _safe_data(ctx, namespace)
    repair = _repair_source(ctx, directory)
    if repair is None:
        baseline = _baseline_source(platform, directory)
        if baseline is not None:
            rendered = baseline
            source = Source.CACHE
        else:
            result = call_ai(
                instruction=_PROMPT,
                data=_bounded_ai_data(base),
                output_model=TerraformDraft,
                prompt_version=PROMPT_VERSION,
                settings=replace(Settings.from_env(), ai_timeout_s=240),
            )
            rendered = _draft_files(result.value)
            source = result.source
    else:
        feedback, previous = repair
        result = call_ai(
            instruction=_REPAIR_INSTRUCTION,
            data=_bounded_ai_data(_repair_data(base, feedback, previous)),
            output_model=TerraformDraft,
            prompt_version=f"{PROMPT_VERSION}-repair",
            settings=replace(Settings.from_env(), ai_timeout_s=240),
        )
        replacements = _draft_files(result.value)
        if not replacements.keys() < previous.keys():
            raise DdakToolError(
                ErrorCode.AI_OUTPUT_INVALID,
                "Terraform 교정은 기존 파일 일부만 교체해야 한다",
            )
        rendered = {**previous, **replacements}
        source = result.source

    files = _write_bundle(directory, rendered)
    return GenerateInfraOutput(
        directory=str(directory),
        layer="platform",
        files=files,
        outputs=_platform_outputs(namespace, platform),
        source=source,
    )
