#!/usr/bin/env python3
"""patch-eval 골격(TODO(O3)).

AI 설정 패치는 P0이다. 일반 실행 기본 OFF를 유지하고 골든 데모에서는 ON으로 검증한다.
AI 패치를 켜기로 하면 쓰는 게이트(💭 이전 안): 10케이스 중 8개 이상 성공, 케이스당 20초 이내.

입력: fixtures/patch_cases/01 ~ 10/ (케이스별 원본 코드, 기대 결과, 설명). 결함은 의도된 것이다.
실행: 케이스마다 var/demo-workspace 사본에 patch_* 툴을 돌리고(실제 LLM, llm 비용 발생),
      기대 결과와 비교한다. 패치 커밋은 사본 안에서만 만든다(팀 저장소에 push 금지).

출력(결정적 JSON, 타임스탬프 없음, 키 정렬):
{"cases": 10, "passed": 8, "per_case_seconds": {"01": 7.2, ...}, "max_seconds": 18.4,
 "gate": {"min_passed": 8, "max_seconds": 20.0}, "gate_passed": true}
종료 코드: 0 게이트 통과, 1 게이트 실패, 3 미구현.
"""

from __future__ import annotations

import json
import sys

NOT_IMPLEMENTED = 3
GATE = {"min_passed": 8, "max_seconds": 20.0}


def main() -> int:
    result = {
        "cases": 0,
        "passed": 0,
        "per_case_seconds": {},
        "max_seconds": None,
        "gate": GATE,
        "gate_passed": False,
        "status": "not_implemented",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("미구현(담당 O3): patch-eval", file=sys.stderr)
    return NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
