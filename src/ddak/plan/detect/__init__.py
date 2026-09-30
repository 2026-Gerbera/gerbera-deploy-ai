"""plan/detect: 변경 탐지·스냅샷 해시. 담당 김준석(O2).

공개 함수: detect_changed_tiers. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/detect 미구현: 담당 김준석"


def detect_changed_tiers(inp: object, ctx: RunContext) -> object:
    """detect_changed_tiers 빈 구현.

    입력(모델 미정): 소스 스냅샷(SnapshotBinding) + 환경별 마지막 성공 배포의 file manifest
    (ddak.core.store). 출력: 바뀐 tier 목록과 스냅샷 해시(ddak.core.snapshots.file_manifest,
    digest_json). git 커밋은 필수가 아니다.
    """
    raise NotImplementedError(_TODO)
