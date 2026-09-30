"""어댑터 규약: "툴 하나 + 환경별 어댑터(local/cloud)" 구조.

- 툴마다 자기 어댑터 Protocol을 TargetAdapter를 넓혀서 정의한다(예: DeployAdapter).
- 어댑터 선택은 실행기가 정한 target과 deploy.yaml, 어댑터 모드로만 한다. AI가 고르지 않는다.
- fake 어댑터는 결정적 정상/실패 시나리오를 돌려준다(테스트, 드라이런, UI 개발).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ddak.core.config import AdapterMode
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode


@runtime_checkable
class TargetAdapter(Protocol):
    """모든 환경별 어댑터의 공통 모양. 툴별 Protocol이 메서드를 더한다."""

    @property
    def target(self) -> Target: ...


@dataclass(frozen=True)
class AdapterSet[A]:
    """툴 하나의 어댑터 생성자 묶음. 없는 환경은 None."""

    local: Callable[[], A] | None = None
    cloud: Callable[[], A] | None = None
    fake: Callable[[Target], A] | None = None


def select_adapter[A](
    adapters: AdapterSet[A],
    target: Target,
    deploy_config: Mapping[str, Any],
    mode: AdapterMode,
) -> A:
    """target과 모드로 어댑터를 고른다.

    TODO(contract): deploy_config에서 해당 target이 활성인지 확인하는 키는 계약 문서가 정한다.
    지금은 인자로만 받아 두고 검사하지 않는다.
    """
    del deploy_config  # TODO(contract)
    if mode is AdapterMode.FAKE:
        if adapters.fake is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "fake 어댑터가 없다")
        return adapters.fake(target)
    factory = adapters.local if target is Target.LOCAL else adapters.cloud
    if factory is None:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"{target.value} 어댑터가 없다")
    return factory()
