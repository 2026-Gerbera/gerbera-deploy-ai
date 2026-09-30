"""Fake 어댑터: 결정적 정상/실패 시나리오(테스트·드라이런·UI 개발).

실제 Docker·AWS를 부르지 않는다.
"""

from __future__ import annotations

from ddak.core.contracts.enums import Target


class FakePingAdapter:
    name = "fake"

    def __init__(self, target: Target) -> None:
        self._target = target

    @property
    def target(self) -> Target:
        return self._target

    def ping(self) -> bool:
        return True
