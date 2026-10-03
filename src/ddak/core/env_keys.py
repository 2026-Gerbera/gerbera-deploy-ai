"""마이그레이션 자격증명은 일회성 migrate 컨테이너에만 전달한다."""

from collections.abc import Iterable

from ddak.core.contracts.errors import DdakToolError, ErrorCode


def is_migration_key(name: str) -> bool:
    return name.upper().endswith("_MIGRATOR")


def check_runtime_keys(keys: Iterable[str]) -> None:
    if any(is_migration_key(key) for key in keys):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID, "앱 런타임에는 마이그레이션 계정을 주입할 수 없다"
        )
