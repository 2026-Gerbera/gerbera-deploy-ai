"""var/runs/<run_id>/ 산출물. 실행기가 쓴다(💭 [I-16]: 메모리 RunContext + 재생·디버깅용 파일).

- context.json: 실행 컨텍스트 스냅샷(RunContext.to_json_dict, redact 후)
- events.jsonl: 진행 이벤트(TODO(O1)). 그 밖의 run 로그 형식은 계약 문서가 정한다.
- 쓰기 전에 항상 redact를 거친다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ddak.core.contracts.base import RUN_ID_PATTERN
from ddak.core.redact import redact_obj

_RUN_ID = re.compile(RUN_ID_PATTERN)
CONTEXT_FILE = "context.json"


def run_dir(base: Path, run_id: str) -> Path:
    """run_id를 검사하고 디렉토리 경로를 돌려준다(경로 탈출 방지)."""
    if not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise ValueError("run_id 형식이 올바르지 않다")
    return base / run_id


def write_context(base: Path, run_id: str, context: dict[str, Any]) -> Path:
    """실행기 전용. 툴 코드에서 부르지 않는다."""
    path = run_dir(base, run_id) / CONTEXT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(redact_obj(context), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_context(base: Path, run_id: str) -> dict[str, Any]:
    path = run_dir(base, run_id) / CONTEXT_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("context.json 최상위는 객체여야 한다")
    return data
