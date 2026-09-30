"""온프렘 어댑터(target=local).

실제 툴이면 여기서 Docker(DOCKER_HOST=unix:// 또는 ssh://)를 부른다.
"""

from __future__ import annotations

from ddak.core.contracts.enums import Target


class LocalPingAdapter:
    name = "local"

    @property
    def target(self) -> Target:
        return Target.LOCAL

    def ping(self) -> bool:
        return True
