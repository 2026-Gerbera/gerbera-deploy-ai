"""실행 문맥(contextvar). 실행기가 툴을 부를 때마다 설정하고 끝나면 되돌린다.

- current_tool: 지금 실행 중인 툴 이름. call_ai 런타임 가드가 이것으로 허용 여부를 판단한다.
  asyncio.to_thread는 contextvar를 복사하므로 동기 툴(워커 스레드)에서도 같은 값이 보인다.
- current_run_id: 로그와 이벤트에 붙일 run_id.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ddak.core.contracts.infra_evidence import BoundaryPolicyVersion

current_tool: ContextVar[str | None] = ContextVar("ddak_current_tool", default=None)
current_run_id: ContextVar[str | None] = ContextVar("ddak_current_run_id", default=None)
_boundary_recorder: ContextVar[Callable[[BoundaryPolicyVersion], None] | None] = ContextVar(
    "ddak_boundary_recorder", default=None
)


def publish_boundary_receipt(value: BoundaryPolicyVersion) -> None:
    """영속화한 경계 영수증을 반환 대기 중인 실행기로 전달한다."""
    recorder = _boundary_recorder.get()
    if recorder is not None:
        recorder(value.model_copy(deep=True))


@contextmanager
def tool_context(
    tool: str,
    run_id: str | None = None,
    *,
    boundary_recorder: Callable[[BoundaryPolicyVersion], None] | None = None,
) -> Iterator[None]:
    """툴 한 번 실행하는 동안 current_tool/current_run_id를 설정한다."""
    tool_token = current_tool.set(tool)
    run_token = current_run_id.set(run_id)
    receipt_token = _boundary_recorder.set(boundary_recorder if tool == "apply_infra" else None)
    try:
        yield
    finally:
        _boundary_recorder.reset(receipt_token)
        current_run_id.reset(run_token)
        current_tool.reset(tool_token)
