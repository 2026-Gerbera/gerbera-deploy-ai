"""환경 무관 공통 로직과 툴별 어댑터 Protocol(TargetAdapter를 넓힌다)."""

from __future__ import annotations

from typing import Protocol

from ddak.core.adapters import TargetAdapter


class PingAdapter(TargetAdapter, Protocol):
    @property
    def name(self) -> str: ...

    def ping(self) -> bool: ...
