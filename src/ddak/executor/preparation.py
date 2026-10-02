"""미등록 환경 툴은 그 트랙만 막는다. 공유 빌드의 미등록은 요청 전체를 막는다."""

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.registry import Registry, UnknownToolError


class MissingToolsError(DdakToolError):
    def __init__(self, names: list[str], message: str) -> None:
        self.missing_tools = sorted(set(names))
        super().__init__(ErrorCode.CONFIG_INVALID, message + ": " + ", ".join(self.missing_tools))


def missing_track_tools(plan: Plan, registry: Registry) -> dict[str, list[str]]:
    def registered(name: str) -> bool:
        try:
            registry.get(name)
            return True
        except UnknownToolError:
            return False

    result: dict[str, list[str]] = {}
    for target in ("local", "cloud"):
        steps = list(getattr(plan.deploy, target).steps)
        steps += [s for s in plan.verify.steps if s.target and s.target.value == target]
        missing = sorted({s.tool for s in steps if not registered(s.tool)})
        if missing:
            result[target] = missing
    for step in plan.build.steps:
        if not registered(step.tool):
            raise MissingToolsError([step.tool], "공유 빌드 툴 미등록")
    return result
