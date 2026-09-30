"""cloud/deploy/secrets.py: Secrets Manager 값 채우기. 담당 안승환(C2).

Terraform이 만든 빈 시크릿에 PutSecretValue(값은 코드 난수, state·AI·화면·로그에 없음).
AI import 금지.
"""

from __future__ import annotations

from collections.abc import Sequence

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext

_TODO = "cloud/deploy 미구현: 담당 안승환"


def put_secret_values(keys: Sequence[str], ctx: RunContext) -> ProviderResult:
    """keys(이름만)의 값을 채운다. 출력: ProviderResult(function="inject_config", keys=...)."""
    raise NotImplementedError(_TODO)
