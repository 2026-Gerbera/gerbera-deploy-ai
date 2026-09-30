"""stderr JSON 한 줄 로그. 앱 코드에서는 print 대신 이것을 쓴다(ruff T20).

필드: ts, level, run_id, component, tool, target, msg (+ 추가 필드). msg와 추가 필드는 redact한다.
component 예: executor, web, plan, infra, ci, cd, verify, core.ai
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from ddak.core import runtime
from ddak.core.redact import redact, redact_obj

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class JsonLogger:
    def __init__(self, component: str, *, stream: TextIO | None = None, level: str | None = None):
        self.component = component
        self._stream = stream
        self._min = _LEVELS.get((level or os.environ.get("DDAK_LOG_LEVEL", "INFO")).upper(), 20)

    def log(self, level: str, msg: str, *, target: str | None = None, **fields: Any) -> None:
        if _LEVELS.get(level, 20) < self._min:
            return
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": level,
            "run_id": runtime.current_run_id.get(),
            "component": self.component,
            "tool": runtime.current_tool.get(),
            "target": target,
            "msg": redact(msg),
        }
        if fields:
            record.update(redact_obj(fields))
        stream = self._stream or sys.stderr
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        stream.flush()

    def debug(self, msg: str, **fields: Any) -> None:
        self.log("DEBUG", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self.log("INFO", msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self.log("WARNING", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self.log("ERROR", msg, **fields)


def get_logger(component: str) -> JsonLogger:
    return JsonLogger(component)
