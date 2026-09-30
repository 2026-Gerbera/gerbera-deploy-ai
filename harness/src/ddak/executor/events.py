"""stream_progress 이벤트 버스(실행기 기능, 등록 함수 아님).

흐름: 실행기 -> EventBus.publish -> 구독자(JSONL 기록, SSE). publish는 detail을 redact한다.
TODO(O1): var/runs/<run_id>/events.jsonl 기록. TODO(C3): SSE 구독자(끊기면 run_id로 JSONL 재생).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ddak.core.contracts.events import RunEvent
from ddak.core.redact import redact

Subscriber = Callable[[RunEvent], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        self._subscribers.append(subscriber)
        return lambda: self._subscribers.remove(subscriber)

    async def publish(self, event: RunEvent) -> None:
        safe = event.model_copy(update={"detail": redact(event.detail)}) if event.detail else event
        for subscriber in list(self._subscribers):
            await subscriber(safe)
