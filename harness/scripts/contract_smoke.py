#!/usr/bin/env python3
"""contract-smoke: 레지스트리 구현 현황 + (TODO(O1)) Fake 1회 호출.

통합 창구 뒤에 main 기준으로 돌린다.

1. ddak.app.load_tools()로 툴 모듈을 모두 등록하고 카탈로그와 비교한다.
   아직 구현하지 않은 툴은 "missing"으로 보고한다(실패가 아니라 진행 상황).
   등록 규칙 위반(이름·위치·시그니처)은 load_tools에서 바로 실패한다.
2. TODO(O1): fixtures/ 입력으로 등록된 툴을 Fake 어댑터 모드에서 1회씩 호출해
   출력 모델 파싱까지 확인한다.

출력(결정적 JSON, 타임스탬프 없음):
{"status": "ok|fail", "registered": [...], "missing": [...], "calls": []}
종료 코드: 0 통과, 1 실패.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    try:
        from ddak.app import load_tools

        registry = load_tools()
    except Exception as exc:  # 등록 규칙 위반 등
        print(
            json.dumps(
                {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            )
        )
        return 1
    result = {
        "status": "ok",
        "registered": sorted(registry.registered()),
        "missing": sorted(registry.missing()),
        "calls": [],  # TODO(O1)
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
