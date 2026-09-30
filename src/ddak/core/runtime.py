"""실행 문맥(contextvar). 실행기가 툴을 부를 때마다 설정하고 끝나면 되돌린다.

- current_tool: 지금 실행 중인 툴 이름. call_ai 런타임 가드가 이것으로 허용 여부를 판단한다.
  asyncio.to_thread는 contextvar를 복사하므로 동기 툴(워커 스레드)에서도 같은 값이 보인다.
- current_run_id: 로그와 이벤트에 붙일 run_id.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current_tool: ContextVar[str | None] = ContextVar("ddak_current_tool", default=None)
current_run_id: ContextVar[str | None] = ContextVar("ddak_current_run_id", default=None)


@contextmanager
def tool_context(tool: str, run_id: str | None = None) -> Iterator[None]:
    """툴 한 번 실행하는 동안 current_tool/current_run_id를 설정한다."""
    tool_token = current_tool.set(tool)
    run_token = current_run_id.set(run_id)
    try:
        yield
    finally:
        current_run_id.reset(run_token)
        current_tool.reset(tool_token)
