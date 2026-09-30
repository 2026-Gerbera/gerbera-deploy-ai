"""stream_progress 이벤트 버스(실행기 기능, 등록 함수 아님).

흐름: 실행기 -> EventBus.publish -> 구독자(JSONL 기록, SSE). publish는 detail을 redact한다.
필수 기록자는 먼저 실행하고 예외를 전파한다. UI는 100ms 뒤 구독 해제하고 취소만 요청한다.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from ddak.core.contracts.events import RunEvent
from ddak.core.redact import redact

Subscriber = Callable[[RunEvent], Awaitable[None]]
_UI_TIMEOUT_S = 0.1


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[Subscriber, bool]] = []
        self._publish_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    def subscribe(self, subscriber: Subscriber, critical: bool = False) -> Callable[[], None]:
        """JSONL 기록자는 critical=True, UI 구독자는 기본값으로 등록한다."""
        entry = (subscriber, critical)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

        return unsubscribe

    async def publish(self, event: RunEvent) -> None:
        safe = event.model_copy(update={"detail": redact(event.detail)}) if event.detail else event
        async with self._publish_lock:
            # 먼저 영속화하고 UI로 보낸다. 기록 실패는 실행기에 전파한다.
            subscribers = list(self._subscribers)
            for subscriber, critical in subscribers:
                if critical:
                    await subscriber(safe)
            for subscriber, critical in subscribers:
                if not critical:
                    task = asyncio.create_task(self._notify(subscriber, safe))
                    self._pending.add(task)
                    task.add_done_callback(self._finished)
                    try:
                        done, _ = await asyncio.wait({task}, timeout=_UI_TIMEOUT_S)
                        if not done:
                            if (subscriber, critical) in self._subscribers:
                                self._subscribers.remove((subscriber, critical))
                            task.cancel()  # 취소를 삼키는 UI의 종료를 기다리지 않는다.
                        elif not task.cancelled():
                            with contextlib.suppress(Exception):
                                task.result()
                    except asyncio.CancelledError:
                        task.cancel()
                        raise

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _notify(subscriber: Subscriber, event: RunEvent) -> None:
        await subscriber(event)
