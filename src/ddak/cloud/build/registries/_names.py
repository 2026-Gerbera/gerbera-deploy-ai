"""저장소 이름·digest 검사(내부). 태그나 `latest`로 배포 참조를 만들지 못하게 한다."""

from __future__ import annotations

import re

from ddak.core.contracts.errors import DdakToolError, ErrorCode

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")  # Docker 태그 규칙


def check_digest(digest: str) -> str:
    if not _DIGEST.fullmatch(digest):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "digest는 sha256:<64hex>만 쓴다(태그 금지)")
    return digest


def check_name(value: str, what: str) -> str:
    if not _NAME.fullmatch(value):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"{what} 이름 형식이 아니다")
    return value


def check_tag(tag: str) -> str:
    if not _TAG.fullmatch(tag) or tag == "latest":
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "태그 형식이 아니다(latest 금지)")
    return tag
