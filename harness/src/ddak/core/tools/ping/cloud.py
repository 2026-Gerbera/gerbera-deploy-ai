"""클라우드 어댑터(target=cloud). 실제 툴이면 여기서 boto3를 부른다(툴 호출마다 새 Session)."""

from __future__ import annotations

from ddak.core.contracts.enums import Target


class CloudPingAdapter:
    name = "cloud"

    @property
    def target(self) -> Target:
        return Target.CLOUD

    def ping(self) -> bool:
        return True
