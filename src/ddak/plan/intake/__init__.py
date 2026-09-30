"""plan/intake: 배포 요청 접수·소스 스냅샷. 담당 김준석(O2).

공개 함수: receive_deploy_request. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI를 import하지 않는다(import-linter 계약).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/intake 미구현: 담당 김준석"


def receive_deploy_request(inp: object, ctx: RunContext) -> object:
    """receive_deploy_request 빈 구현.

    입력(모델 미정, core/contracts/deploy_request.py 자리): 채팅 의도 JSON을 코드가 확인한 배포
    요청(대상 local|cloud|both, 프로젝트, 소스 위치). 출력: 요청 ID + 소스 스냅샷
    (SnapshotBinding, core/contracts/release.py). 스냅샷은 ddak.core.snapshots를 쓴다.
    """
    raise NotImplementedError(_TODO)
