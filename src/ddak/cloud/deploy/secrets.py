"""cloud/deploy/secrets.py: Secrets Manager 값 채우기. 담당 안승환(C2).

Terraform이 만든 빈 시크릿에 PutSecretValue(값은 코드 난수, state·AI·화면·로그에 없음).
AI import 금지.

내부 API(fill_secret)는 시크릿 이름을 인자로 받는다. 앱 시크릿 이름(Terraform 출력)이 정해지면
put_secret_values가 ctx에서 읽어 부른다(💭 확정 필요).

- 앱 시크릿 하나에 JSON 객체({"키": "값"})로 담는다. ECS는 valueFrom `<ARN>:<키>::`로 읽는다.
- 온프렘 inject_config와 같은 규칙: SECRET_KEY만 코드가 만든다(token_hex(32), 있으면 재사용).
  다른 키는 운영자가 미리 넣어 둔 값이 있어야 한다. 없으면 실패한다.
- 값은 반환·로그·오류 메시지에 넣지 않는다. 결과는 키 이름만.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddak.cd.interface import ProviderResult
from ddak.cloud.deploy._aws import call
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode

_TODO = "cloud/deploy 미구현: 담당 안승환"

GENERATED = "SECRET_KEY"
_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_KEY_VALUE = re.compile(r"^[0-9a-f]{64}$")


class SecretsClient(Protocol):
    def describe_secret(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SecretFilled:
    keys: list[str]  # 요청한 키(이름만)
    generated: list[str]  # 이번에 새로 만든 키
    changed: bool


def fill_secret(
    client: SecretsClient,
    secret_id: str,
    keys: Sequence[str],
    *,
    token: Callable[[], str] = lambda: secrets.token_hex(32),
) -> SecretFilled:
    """keys가 모두 값을 갖게 한다. 새로 만들 것이 있을 때만 새 버전을 쓴다."""
    if not keys:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "채울 키가 없다")
    if len(set(keys)) != len(keys) or any(not _KEY.fullmatch(k) for k in keys):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "키 이름 형식 오류 또는 중복")

    values = _current(client, secret_id)
    missing = [k for k in keys if k != GENERATED and not values.get(k)]
    if missing:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            f"운영자가 넣어야 하는 키가 비어 있다: {', '.join(missing)}",
        )
    generated: list[str] = []
    if GENERATED in keys:
        existing = values.get(GENERATED)
        if existing is None:
            values[GENERATED] = token()
            generated.append(GENERATED)
        elif not _SECRET_KEY_VALUE.fullmatch(existing):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "기존 SECRET_KEY는 64자리 hex여야 한다")
    if generated:
        body = json.dumps(values, sort_keys=True)
        call(
            "시크릿 값을 쓰지 못했다",
            lambda: client.put_secret_value(SecretId=secret_id, SecretString=body),
        )
    return SecretFilled(keys=list(keys), generated=generated, changed=bool(generated))


def _current(client: SecretsClient, secret_id: str) -> dict[str, str]:
    """현재 값(JSON 객체). Terraform이 값 없이 만든 시크릿이면 빈 객체."""
    described = call("시크릿을 찾지 못했다", lambda: client.describe_secret(SecretId=secret_id))
    if described.get("DeletedDate"):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "시크릿이 삭제 예정 상태다")
    stages = described.get("VersionIdsToStages") or {}
    if not any("AWSCURRENT" in s for s in stages.values()):
        return {}
    raw = call("시크릿 값을 읽지 못했다", lambda: client.get_secret_value(SecretId=secret_id)).get(
        "SecretString"
    )
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = None  # 원문(비밀값)을 오류에 싣지 않는다
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "시크릿 값이 문자열 JSON 객체가 아니다")
    return dict(parsed)


def put_secret_values(keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    """keys(이름만)의 값을 채운다. 출력: ProviderResult(function="inject_config", keys=...)."""
    raise NotImplementedError(_TODO)
