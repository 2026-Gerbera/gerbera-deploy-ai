"""cloud/deploy/secrets.py: Secrets Manager 값 채우기. 담당 안승환(C2).

Terraform이 만든 빈 시크릿에 PutSecretValue(값은 코드 난수, state·AI·화면·로그에 없음).
AI import 금지.

- 앱 시크릿은 키마다 하나다. Terraform 앱 출력 `app_secret_arn_<KEY>`(core/contracts/infra_outputs,
  PR #8 기준)가 그 ARN이다. ECS는 valueFrom `<ARN>`으로 값 전체를 읽는다.
- 내부 API(fill_secrets)는 {키: 시크릿 ARN}을 인자로 받는다. ctx 연결은 put_secret_values가 한다.
- 온프렘 inject_config와 같은 규칙: SECRET_KEY는 코드가 만든다(token_hex(32), 있으면 재사용).
  호출자가 generated_values로 만든 값을 준 키(예: DATABASE_URL)도 비어 있을 때만 쓴다.
  다른 키는 운영자가 미리 넣은 값(AWSCURRENT)이 있어야 한다. 없으면 아무것도 쓰지 않고 실패한다.
- 운영자 키는 값을 읽지 않고 버전 존재만 본다. SECRET_KEY만 형식 확인을 위해 읽는다.
- 값은 반환·로그·오류 메시지에 넣지 않는다. 결과는 키 이름만.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddak.cloud.deploy._aws import call
from ddak.core.contracts.errors import DdakToolError, ErrorCode

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


def fill_secrets(
    client: SecretsClient,
    keys: Sequence[str],
    secret_ids: Mapping[str, str],
    *,
    token: Callable[[], str] = lambda: secrets.token_hex(32),
    generated_values: Mapping[str, str] | None = None,
) -> SecretFilled:
    """keys마다 시크릿에 현재 값이 있게 한다. 코드가 만드는 값은 SECRET_KEY와 generated_values다."""
    made = dict(generated_values or {})
    if GENERATED in made or any(not isinstance(v, str) or not v for v in made.values()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "생성 값 형식 오류")
    if not keys:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "채울 키가 없다")
    if len(set(keys)) != len(keys) or any(not _KEY.fullmatch(k) for k in keys):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "키 이름 형식 오류 또는 중복")
    unmapped = [k for k in keys if not secret_ids.get(k)]
    if unmapped:
        raise DdakToolError(
            ErrorCode.INFRA_MISSING,
            f"시크릿 출력(app_secret_arn_<KEY>)이 없다: {', '.join(unmapped)}",
        )

    # 먼저 전부 확인하고, 운영자 키가 하나라도 비었으면 아무것도 쓰지 않는다.
    has_value = {k: _has_current(client, secret_ids[k]) for k in keys}
    missing = [k for k in keys if k != GENERATED and k not in made and not has_value[k]]
    if missing:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            f"운영자가 넣어야 하는 키가 비어 있다: {', '.join(missing)}",
        )
    generated: list[str] = []
    if GENERATED in keys:
        secret_id = secret_ids[GENERATED]
        if has_value[GENERATED]:
            existing = call(
                "시크릿 값을 읽지 못했다", lambda: client.get_secret_value(SecretId=secret_id)
            ).get("SecretString")
            if not isinstance(existing, str) or not _SECRET_KEY_VALUE.fullmatch(existing):
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "기존 SECRET_KEY는 64자리 hex여야 한다"
                )
        else:
            value = token()
            call(
                "시크릿 값을 쓰지 못했다",
                lambda: client.put_secret_value(SecretId=secret_id, SecretString=value),
            )
            generated.append(GENERATED)
    for key in keys:
        if key not in made or has_value[key]:
            continue
        secret_id, value = secret_ids[key], made[key]
        call(
            "시크릿 값을 쓰지 못했다",
            lambda secret_id=secret_id, value=value: client.put_secret_value(
                SecretId=secret_id, SecretString=value
            ),
        )
        generated.append(key)
    return SecretFilled(keys=list(keys), generated=generated, changed=bool(generated))


def _has_current(client: SecretsClient, secret_id: str) -> bool:
    """AWSCURRENT 버전이 있는가. Terraform이 값 없이 만든 시크릿이면 False."""
    described = call("시크릿을 찾지 못했다", lambda: client.describe_secret(SecretId=secret_id))
    if described.get("DeletedDate"):
        raise DdakToolError(ErrorCode.INFRA_MISSING, "시크릿이 삭제 예정 상태다")
    stages = described.get("VersionIdsToStages") or {}
    return any("AWSCURRENT" in s for s in stages.values())
