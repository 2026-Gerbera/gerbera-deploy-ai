"""실행별 AWS 프로필 선택과 계정 확인. 환경 변수와 자격증명 파일은 수정하지 않는다."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import boto3
from botocore.config import Config

from ddak.core import defaults
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def aws_settings(project_settings: Mapping[str, Any] | None = None) -> dict[str, str]:
    """run 스냅샷 우선, 누락/None이면 비밀 없는 파일 기본값. 잘못된 값은 거부한다."""
    values = project_settings or {}
    try:
        profile = values.get("aws_profile")
        if profile is None:
            profile = defaults.load_defaults().get("aws_profile")
        account = values.get("aws_expected_account_id")
        if account is None:
            account = defaults.load_aws_defaults().get("expected_account_id")
        if (
            not isinstance(profile, str)
            or not profile.strip()
            or profile != profile.strip()
            or any(ord(c) < 32 or c in "[]" for c in profile)
            or not isinstance(account, str)
            or re.fullmatch(r"[0-9]{12}", account) is None
        ):
            raise ValueError
    except Exception:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "AWS 프로필과 기대 계정 설정을 확인해 주세요",
            needs_human=True,
        ) from None
    return {"aws_profile": profile, "aws_expected_account_id": account}


def session_credentials(session: Any) -> dict[str, str | None]:
    """SDK 자격증명을 고정한다. 원문 예외와 값은 오류 경로로 내보내지 않는다."""
    try:
        current = session.get_credentials()
        frozen = current.get_frozen_credentials()
        if (
            not isinstance(frozen.access_key, str)
            or not frozen.access_key
            or not isinstance(frozen.secret_key, str)
            or not frozen.secret_key
            or (frozen.token is not None and not isinstance(frozen.token, str))
        ):
            raise ValueError
        return {
            "aws_access_key_id": frozen.access_key,
            "aws_secret_access_key": frozen.secret_key,
            "aws_session_token": frozen.token or None,
        }
    except Exception:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "AWS 자격증명을 확보하지 못했습니다. 선택한 프로필의 로그인 상태를 확인해 주세요",
            needs_human=True,
        ) from None


def checked_session(
    project_settings: Mapping[str, Any] | None = None,
    *,
    region_name: str | None = None,
    config: Config | None = None,
    session_factory: Callable[..., Any] | None = None,
    credentials: Mapping[str, str | None] | None = None,
) -> Any:
    """선택 프로필의 STS 계정을 확인한 세션만 반환한다. 명시적 키도 같은 검사를 받는다."""
    settings = aws_settings(project_settings)
    kwargs: dict[str, Any] = {"profile_name": settings["aws_profile"]}
    if region_name is not None:
        kwargs["region_name"] = region_name
    if credentials is not None:
        if (
            set(credentials) != {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
            or not credentials["aws_access_key_id"]
            or not credentials["aws_secret_access_key"]
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "AWS 명시적 자격증명을 확인해 주세요")
        kwargs.update(credentials)
    try:
        factory = session_factory or boto3.Session
        if credentials is None:
            # 자동 갱신/환경값이 STS 검사와 서비스 호출 사이에 다른 계정으로 바뀌지 않는다.
            kwargs.update(session_credentials(factory(**kwargs)))
        session = factory(**kwargs)
        identity = session.client(
            "sts",
            config=config
            or Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
        ).get_caller_identity()
        account = identity["Account"]
        if not isinstance(account, str) or re.fullmatch(r"[0-9]{12}", account) is None:
            raise ValueError
    except DdakToolError:
        raise
    except Exception:
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "AWS 계정을 확인하지 못했습니다. 선택한 프로필의 로그인 상태와 연결을 확인해 주세요",
            needs_human=True,
        ) from None
    if account != settings["aws_expected_account_id"]:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            "선택한 AWS 프로필의 계정이 기대 계정과 다릅니다. AWS 연결 설정을 확인해 주세요",
            needs_human=True,
        )
    return session
