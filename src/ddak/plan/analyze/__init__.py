"""plan/analyze: 프로젝트 분석·환경 키 분류(Jev/Claude). 담당 김준석(O2).

공개 함수: analyze_project. 다른 디렉토리는 이 파일의 공개 함수만 쓴다.
AI 호출은 ddak.core.ai(call_ai, ask_jev)로만 한다(허용 디렉토리, import-linter 계약 2).
빈 구현이다. 구현이 끝나면 이 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
(시그니처: inp: <Tool>Input, ctx: RunContext -> <Tool>Output, 모델은 ddak.core.contracts.tools).
"""

from __future__ import annotations

from ddak.core.contracts.context import RunContext

_TODO = "plan/analyze 미구현: 담당 김준석"


def analyze_project(inp: object, ctx: RunContext) -> object:
    """analyze_project 빈 구현.

    입력(모델 미정): 소스 스냅샷 + deploy.yaml(DeployConfig, core/contracts/deploy_config.py 자리).
    출력: tier·환경 키 분류 결과(규칙이 바닥, AI는 제안만). AI 결과에는 source 라벨과 AIUsage
    (core/contracts/base.py)를 남긴다. call_ai 입력은 redact를 통과해야 한다.
    """
    raise NotImplementedError(_TODO)
