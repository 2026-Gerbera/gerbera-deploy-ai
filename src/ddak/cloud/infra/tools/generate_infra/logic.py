"""AI가 리소스 HCL만 제안하고 코드가 파일·출력 선언을 고정한다."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import Field

from ddak.core.ai.gateway import call_ai
from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput
from ddak.core.snapshots import digest_bytes

PROMPT_VERSION = "infra-aws-v1"
_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
_FILE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\.tf$")
_NAMESPACE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class TerraformDraft(ContractModel):
    files: dict[str, str] = Field(min_length=1, max_length=16)


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
    }


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
        f"project={ctx.project}\n"
        f"domain={domain}\n"
        f"hosted_zone_id={zone}\n"
        f"repository_url={ctx.repo_url.removesuffix('.git')}\n"
        f"dockerhub_namespace={namespace}\n"
        "region=ap-northeast-2\n"
        "availability_zones=ap-northeast-2a,ap-northeast-2c"
    )


def generate_infra(inp: GenerateInfraInput, ctx: RunContext) -> GenerateInfraOutput:
    if inp.run_id != ctx.run_id:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "인프라 생성 run ID가 다르다")
    if inp.layer != "platform":
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "앱 인프라 갱신에는 새 시크릿 키 목록 계약이 필요하다",
            needs_human=True,
        )
    namespace = os.environ.get("DDAK_DOCKERHUB_NAMESPACE", "").strip()
    if not _NAMESPACE.fullmatch(namespace):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "DDAK_DOCKERHUB_NAMESPACE 설정이 필요하다")
    directory = Path(inp.directory).resolve()
    if not directory.is_dir() or directory.is_symlink() or any(directory.iterdir()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "비어 있는 인프라 번들 디렉터리가 필요하다")
    result = call_ai(
        instruction=_PROMPT,
        data=_safe_data(ctx, namespace),
        output_model=TerraformDraft,
        prompt_version=PROMPT_VERSION,
    )
    files: dict[str, str] = {}
    total = 0
    for name, source in result.value.files.items():
        if not _FILE.fullmatch(name) or not source.strip():
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "Terraform 파일 이름/내용 오류")
        raw = source.encode("utf-8")
        total += len(raw)
        if len(raw) > 256 * 1024 or total > 1024 * 1024:
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "Terraform 생성 결과 크기 초과")
        try:
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        except OSError as exc:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "Terraform 번들을 쓸 수 없다") from exc
        files[name] = digest_bytes(raw)
    return GenerateInfraOutput(
        directory=str(directory),
        layer="platform",
        files=files,
        outputs=_platform_outputs(namespace, ctx.project),
        source=result.source,
    )
