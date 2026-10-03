"""verify/diagnose: 원인 분석(설명 전용). 담당 장민영(O3).

공개 함수: diagnose_parity_gap(툴 진입점, tool.py가 등록), diagnose_by_rules(규칙 부분).
다른 디렉토리는 이 파일의 공개 이름만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
실행기가 compare 불합격·트랙 실패 뒤에만 부르고, 출력은 판정·롤백에 쓰지 않는다
(2026-10-03 diagnose-advisory 결정). 입출력은 core/contracts/tools/diagnose_parity_gap.py.
"""

from __future__ import annotations

from ddak.verify.diagnose.advisory import diagnose_parity_gap
from ddak.verify.diagnose.rules import RuleDiagnosis, diagnose_by_rules

__all__ = ["RuleDiagnosis", "diagnose_by_rules", "diagnose_parity_gap"]
