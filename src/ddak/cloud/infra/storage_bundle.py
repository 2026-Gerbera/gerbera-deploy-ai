"""저장소 번들의 근거·승인 요약·기준본. 생성기나 외부 호출에 의존하지 않는다."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.project_settings import CLOUD_PLATFORM_PATTERN, cloud_platform_name
from ddak.core.redact import redact
from ddak.core.snapshots import digest_bytes
from ddak.core.storage import STORAGE_ENV_KEY, valid_bucket

STORAGE_PROMPT_VERSION = "infra-app-storage-v2"
MAX_STORAGE_HCL_BYTES = 3072
MAX_STORAGE_SUMMARY_BYTES = 6144
MAX_STORAGE_EVIDENCE = 8
MAX_RATIONALE_BYTES = 240
_FILE = "storage.tf"
_HEADER = "# ddak-storage "
_MAX_BUNDLE_BYTES = 6144
_KINDS = {"hardcoded_dir", "env_read", "file_write"}


def storage_request(ctx: RunContext) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    """이번 UPDATE의 저장소 입력에서 허용된 필드만 선택한다."""
    request = ctx.project_settings.get("_infra_storage")
    if (
        ctx.mode != RunMode.UPDATE
        or not isinstance(request, Mapping)
        or request.get("intent") not in ("create", "remove")
    ):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "저장소는 app UPDATE의 create/remove 지정이 필요하다"
        )
    platform = cloud_platform_name(ctx.project, ctx.project_settings)
    account = ctx.project_settings.get("aws_expected_account_id")
    if account is None and ctx.adapter_mode == AdapterMode.FAKE:
        account = "123456789012"
    if not re.fullmatch(CLOUD_PLATFORM_PATTERN, platform) or not isinstance(account, str):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 플랫폼/계정 형식 오류")
    if not re.fullmatch(r"[0-9]{12}", account):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 계정은 12자리여야 한다")
    cloud = ctx.platform.get("cloud", {})
    role_arn = cloud.get("task_role_arn") if isinstance(cloud, Mapping) else None
    role = f"{platform}-task"
    if role_arn is not None:
        match = re.fullmatch(
            rf"arn:aws:iam::{account}:role/(?:[A-Za-z0-9+=,.@_-]+/)*"
            r"([A-Za-z0-9+=,.@_-]{1,64})",
            role_arn if isinstance(role_arn, str) else "",
        )
        if match is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 태스크 역할 형식 오류")
        role = match[1]
    binding = {"platform": platform, "account": account, "task_role_name": role}
    bucket = request.get("bucket")
    if not isinstance(bucket, str) or not valid_bucket(platform, bucket):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "예약된 저장소 버킷 이름이 필요하다")
    if request["intent"] == "remove" and cloud.get("upload_bucket") != bucket:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "제거할 버킷이 현재 출력과 다르다")
    evidence = _evidence(request.get("evidence", []))
    if request["intent"] == "create" and not evidence:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 생성에는 소스 근거가 필요하다")
    return request["intent"], binding, evidence


def _evidence(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, (list, tuple)):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 근거 형식 오류")
    selected = []
    for item in items[:MAX_STORAGE_EVIDENCE]:
        # AST 탐지 결과인 dataclass와 전달용 dict를 모두 받되 값/코드는 선택하지 않는다.
        file = item.get("file") if isinstance(item, Mapping) else getattr(item, "file", None)
        line = item.get("line") if isinstance(item, Mapping) else getattr(item, "line", None)
        kind = item.get("kind") if isinstance(item, Mapping) else getattr(item, "kind", None)
        if (
            not isinstance(file, str)
            or not file
            or len(file.encode("utf-8")) > 96
            or Path(file).is_absolute()
            or ".." in Path(file).parts
            or any(ord(char) < 32 for char in file)
            or isinstance(line, bool)
            or not isinstance(line, int)
            or not 1 <= line <= 1_000_000
            or kind not in _KINDS
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 근거 file/line/kind 형식 오류")
        selected.append({"file": file, "line": line, "kind": kind})
    return selected


def _rationale(items: Any) -> list[str]:
    if (
        not isinstance(items, (list, tuple))
        or len(items) != 5
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.encode("utf-8")) > MAX_RATIONALE_BYTES
            or "\n" in item
            or "\r" in item
            for item in items
        )
    ):
        raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "저장소 근거는 짧은 5단계 문장이어야 한다")
    return list(items)


def storage_source(source: Source | str) -> str:
    if source == Source.REPLAY:
        return Source.FIXTURE.value
    if source not in (Source.LIVE, Source.CACHE, Source.FIXTURE):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 출처 형식 오류")
    return str(source)


def storage_bundle(
    hcl: str, ctx: RunContext, rationale: tuple[str, ...], source: Source | str
) -> dict[str, str]:
    intent, binding, evidence = storage_request(ctx)
    if not hcl.strip() or len(hcl.encode("utf-8")) > MAX_STORAGE_HCL_BYTES:
        raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "저장소 HCL은 3KiB 이하여야 한다")
    metadata = {
        "version": STORAGE_PROMPT_VERSION,
        "intent": intent,
        "binding": binding,
        "rationale": _rationale(rationale),
        "evidence": evidence,
        "source": storage_source(source),
    }
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    files = {_FILE: _HEADER + header + "\n" + hcl}
    _read_bundle(files)
    storage_summary(files, ctx, source)
    return files


def _read_bundle(files: Mapping[str, str]) -> tuple[dict[str, Any], str]:
    if set(files) != {_FILE} or not isinstance(files[_FILE], str):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들은 storage.tf 하나여야 한다")
    content = files[_FILE]
    if len(content.encode("utf-8")) > _MAX_BUNDLE_BYTES:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 크기 초과")
    header, separator, hcl = content.partition("\n")
    if not header.startswith(_HEADER) or not separator:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 근거가 없다")
    try:
        metadata = json.loads(header[len(_HEADER) :])
    except ValueError:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 근거 형식 오류") from None
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"version", "intent", "binding", "rationale", "evidence", "source"}
        or metadata["version"] != STORAGE_PROMPT_VERSION
        or metadata["intent"] not in ("create", "remove")
        or not isinstance(metadata["binding"], dict)
        or set(metadata["binding"]) != {"platform", "account", "task_role_name"}
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 버전/입력 형식 오류")
    binding = metadata["binding"]
    if (
        not isinstance(binding["platform"], str)
        or not re.fullmatch(CLOUD_PLATFORM_PATTERN, binding["platform"])
        or not isinstance(binding["account"], str)
        or not re.fullmatch(r"[0-9]{12}", binding["account"])
        or not isinstance(binding["task_role_name"], str)
        or not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", binding["task_role_name"])
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 계정/플랫폼/역할 형식 오류")
    _rationale(metadata["rationale"])
    _evidence(metadata["evidence"])
    storage_source(metadata["source"])
    if not hcl.strip() or len(hcl.encode("utf-8")) > MAX_STORAGE_HCL_BYTES or _HEADER in hcl:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 HCL 크기/형식 오류")
    return metadata, hcl


def storage_summary(
    files: Mapping[str, str], ctx: RunContext, source: Source | str
) -> dict[str, Any]:
    """HCL 본문 → summary['storage']. 병합한 전체 요약의 8KiB 검사는 호출자가 한다."""
    intent, binding, evidence = storage_request(ctx)
    metadata, hcl = _read_bundle(files)
    if metadata["intent"] != intent or metadata["binding"] != binding:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 번들 입력이 이번 실행과 다르다")
    account = binding["account"]

    def display(text: str) -> str:
        return redact(text.replace(account, "************"), max_len=None)

    bucket = ctx.project_settings["_infra_storage"]["bucket"]
    summary = {
        "intent": intent,
        "rationale": [display(item) for item in metadata["rationale"]],
        "evidence": [{**item, "file": display(item["file"])} for item in evidence],
        "bucket": bucket,
        "env": {STORAGE_ENV_KEY: f"s3://{bucket}/img" if intent == "create" else "img"},
        "files": {_FILE: display(hcl)},
        "source": storage_source(source),
    }
    # ensure_ascii=True인 소비자도 같은 상한을 지키도록 둘 다 확인한다.
    if any(
        len(json.dumps(summary, ensure_ascii=ascii_mode).encode("utf-8"))
        > MAX_STORAGE_SUMMARY_BYTES
        for ascii_mode in (False, True)
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 승인 요약은 6KiB 이하여야 한다")
    return summary


def _baseline_path(root: Path, platform: str, binding: dict[str, str]) -> Path:
    if platform != binding["platform"]:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본 플랫폼이 다르다")
    key = digest_bytes(json.dumps(binding, sort_keys=True).encode()).removeprefix("sha256:")
    path = Path(root) / "infra-baselines" / platform / STORAGE_PROMPT_VERSION / key
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본 symlink 경로 오류")
    return path


def load_storage_baseline(root: Path, ctx: RunContext) -> dict[str, str] | None:
    """service.root에서 이번 입력에 묶인 create 기준본만 읽는다. 다른 입력은 cache miss."""
    intent, binding, _ = storage_request(ctx)
    if intent != "create":
        return None
    path = _baseline_path(root, binding["platform"], binding)
    if not path.exists():
        return None
    if not path.is_dir() or {p.name for p in path.iterdir()} != {_FILE}:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본 파일 목록 오류")
    file = path / _FILE
    if file.is_symlink() or not file.is_file() or file.stat().st_size > _MAX_BUNDLE_BYTES:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본 파일 형식/크기 오류")
    try:
        files = {_FILE: file.read_text(encoding="utf-8")}
    except (OSError, UnicodeError):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본을 읽을 수 없다") from None
    metadata, _ = _read_bundle(files)
    if metadata["binding"] != binding or metadata["intent"] != "create":
        return None
    if metadata["source"] != Source.LIVE:
        return None
    storage_summary(files, ctx, Source.CACHE)
    return files


def save_storage_baseline(root: Path, platform: str, files: Mapping[str, str]) -> None:
    """LIVE create 관문·전체 요약 통과 뒤에만 호출한다. 생성기 자체는 저장하지 않는다."""
    metadata, _ = _read_bundle(files)
    if metadata["intent"] != "create" or metadata["source"] != Source.LIVE:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "LIVE create만 저장소 기준본으로 저장한다")
    path = _baseline_path(root, platform, metadata["binding"])
    try:
        path.mkdir(parents=True, exist_ok=True)
        destination = path / _FILE
        if destination.is_symlink():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "저장소 기준본 symlink 파일 오류")
        with tempfile.NamedTemporaryFile(dir=path, prefix=".storage-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(files[_FILE].encode("utf-8"))
                stream.flush()
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "저장소 기준본을 저장할 수 없다") from None
