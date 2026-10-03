"""승인 화면용 임시 C-18 자료. 공유 툴 스키마나 인프라 검증기를 대체하지 않는다."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import Sha256
from ddak.core.redact import redact_obj

# 10/3 8KiB→16KiB: 첫 구축 인프라 요약(기반 확보·권한 경계 포함)이 실제 AWS에서 8KiB를 넘었다.
MAX_META_BYTES = 16384
Count = Annotated[int, Field(ge=0)]
_MASKED_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(?:\*{3,12}|●{3}):[\w/*?.:@+=-]+")


def _redaction_input(value: Any) -> Any:
    # 가린 ARN의 resource type 'secret:'을 key=value 비밀값으로 오인하지 않는다.
    # 계정 ID가 가려진 ARN 전체와 일치할 때만 적용하고 나머지 내용은 계속 검사한다.
    if isinstance(value, str) and _MASKED_ARN.fullmatch(value):
        return value.replace(":secret:", ":resource:", 1)
    if isinstance(value, dict):
        return {k: _redaction_input(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redaction_input(v) for v in value]
    return value


class _Meta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _PatchMeta(_Meta):
    reason: Annotated[str, Field(min_length=1, max_length=200)]
    reuse: bool
    source: Source
    passed: bool | None = None
    patch_sha256: Sha256 | None = None
    new_env_keys: list[str] = Field(default_factory=list)
    gitleaks: str | None = None
    patterns: list[str] = Field(default_factory=list)
    target_hashes: dict[str, Sha256] = Field(default_factory=dict)


class _Counts(_Meta):
    create: Count
    update: Count
    delete: Count
    replace: Count


class _Analyzer(_Meta):
    errors: Count
    security_warnings: Count


class _Checkov(_Meta):
    passed: bool
    failed: list[str]


class _InfraSummary(_Meta):
    # 01 공통 계약 C-18의 C1/C3 역할 문서 예시. 정책 판정은 C1/O2 책임이다.
    layer: Literal["app", "platform"]
    plan_sha256: Sha256
    exit_code: Literal[0, 2]
    headline: Annotated[str, Field(min_length=1)]
    counts: _Counts
    destructive: list[str | dict[str, Any]]
    iam_diff: list[dict[str, Any]]
    access_analyzer: _Analyzer
    checkov: _Checkov
    sensitive_masked: Literal[True]

    @field_validator("exit_code", "sensitive_masked", mode="before")
    @classmethod
    def exact_literal_type(cls, value: Any, info: Any) -> Any:
        expected = int if info.field_name == "exit_code" else bool
        if type(value) is not expected:
            raise ValueError("literal type")
        return value


def encode_meta(value: dict[str, Any] | None, *, infra: bool = False) -> str:
    """16KiB 이하 JSON을 검증하고 불변 문자열로 저장한다. 원문 오류는 노출하지 않는다."""
    if value is None:
        return "null"
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_META_BYTES:
            raise ValueError("size")
        model = _InfraSummary if infra else _PatchMeta
        data = model.model_validate_json(encoded).model_dump(mode="json", exclude_unset=True)
        checked = _redaction_input(data)
        if redact_obj(checked, max_len=MAX_META_BYTES) != checked:
            raise ValueError("redaction")
        result = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if len(result.encode("utf-8")) > MAX_META_BYTES:
            raise ValueError("size")
        return result
    except (ValueError, TypeError, RecursionError):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "승인 메타 형식/크기 오류 또는 가리지 않은 민감 정보가 있다",
        ) from None


def check_infra_summary(encoded: str, subject: str | None) -> None:
    if subject is not None:
        summary = json.loads(encoded)
        if summary is None or summary["plan_sha256"] != subject:
            raise DdakToolError(
                ErrorCode.APPROVAL_REQUIRED, "인프라 요약이 없거나 승인 대상 해시와 다르다"
            )
