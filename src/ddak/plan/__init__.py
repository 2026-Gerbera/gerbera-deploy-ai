"""plan: ① 플랜 + ② 계획 검증. 담당 O2(김준석), patch·dockerfile은 O3(장민영).

하위 디렉토리(각 __init__.py의 공개 함수만 다른 곳에서 쓴다):
- intake/ receive_deploy_request · detect/ detect_changed_tiers · analyze/ analyze_project(AI)
- planner/ generate_plan(AI) · validate/ validate_plan(결정적 검사기)
- patch/ patch_config·patch_db_access·patch_storage(AI, 토글 ON 전용)
- dockerfile/ generate_dockerfile(AI, generate.py) · validate_dockerfile(검사기, validate.py)

- AI 호출은 ddak.core.ai(call_ai, ask_jev)로만, analyze·planner·patch·dockerfile에서만(계약 2).
  제안(JSON, Dockerfile 초안)만 만들고 실행하지 않는다.
- validate/와 dockerfile/validate.py는 결정적 검사기다. AI와 생성기(planner,
  dockerfile/generate.py)를 import하지 않는다(계약 5).
- 툴 등록: 구현이 끝나면 그 디렉토리에 tool.py를 만들고 @tool("<이름>")으로 등록한다
  (ddak.app이 자동 탐색). 빈 구현은 등록하지 않는다.
"""
