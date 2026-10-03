"""계획 준비 중 읽기 세션으로 이름을 예약하고 승인 컨텍스트에 묶는다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ddak.core.aws_credentials import checked_session
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.project_settings import cloud_platform_name
from ddak.core.storage import OUTPUT_KEY
from ddak.core.storage_sequence import remember_bucket, reserve_bucket


def prepare_storage_context(
    ctx: RunContext, root: Path, *, probe: Callable[[str], int] | None = None
) -> RunContext:
    request = ctx.project_settings.get("_infra_storage")
    if not isinstance(request, Mapping) or ctx.targets == "onprem":
        return ctx
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    if request.get("intent") == "remove":
        bucket = ctx.platform.get("cloud", {}).get(OUTPUT_KEY)
        if not isinstance(bucket, str) or request.get("bucket") not in (None, bucket):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "제거할 버킷이 현재 출력과 다르다")
        remember_bucket(root, ctx.project, platform, bucket)
    elif request.get("intent") == "create":
        if probe is None:
            probe = (lambda _: 404) if ctx.adapter_mode is AdapterMode.FAKE else _probe(ctx)
        bucket = reserve_bucket(root, ctx.project, platform, ctx.run_id, probe)
    else:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 생성·제거 의도가 필요하다")
    return replace(
        ctx,
        project_settings={
            **ctx.project_settings,
            "_infra_storage": {**request, "bucket": bucket},
        },
    )


def _probe(ctx: RunContext) -> Callable[[str], int]:
    config = Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 0})
    try:
        session = checked_session(
            ctx.project_settings,
            region_name="ap-northeast-2",
            config=config,
            session_factory=boto3.Session,
        )
        client = session.client("s3", config=config)
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "버킷 이름 조회 세션 준비 실패") from None

    def head(bucket: str) -> int:
        try:
            client.head_bucket(Bucket=bucket)
            return 200
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (403, 404):
                return status
        except Exception:
            raise DdakToolError(
                ErrorCode.ADAPTER_FAILED, "버킷 이름 조회 실패; 연결 상태를 확인하세요"
            ) from None
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "버킷 이름 조회 실패; 연결 상태를 확인하세요")

    return head
