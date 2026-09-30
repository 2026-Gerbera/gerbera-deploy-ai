"""onprem/provision: 온프렘 서버·컨테이너 준비, 앱 DB·계정. 담당 김준석(O2).

공개 함수: prepare_host, ensure_app_database. 계약 모델은 아직 없다(입출력은 docstring).
AI import 금지(import-linter 계약 1). 빈 구현이다.
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "onprem/provision 미구현: 담당 김준석"


def prepare_host(ctx: RunContext) -> object:
    """Docker 엔진·네트워크·db tier(compose/local) 준비 확인. 출력: 준비 결과(모델 미정)."""
    raise NotImplementedError(_TODO)


def ensure_app_database(ctx: RunContext) -> object:
    """앱 DB·최소 권한 계정 준비(값은 env 파일에만). 출력: DB 준비 결과(모델 미정)."""
    raise NotImplementedError(_TODO)
