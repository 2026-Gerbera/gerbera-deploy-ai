# plan/analyze (담당: 김준석)
- 할 일: tier·Dockerfile 유무·환경 키(plain/secret) 분석(`analyze_project`). 입출력: `core/contracts/tools/analyze_project.py`.
- `rules.py`: env_example `NAME=` 줄과 소스 `os.environ/os.getenv` 정규식으로 키 추출(값은 읽지 않음), 이름 패턴 분류. AI 없음.
- `logic.py`: 규칙이 바닥. 애매한 키만 `ask_jev`(noul 질문)에 묻는다. Jev는 secret으로 올리기만 가능(규칙 secret은 질문 대상이 아님).
  Jev 불가면 애매한 키는 secret(보수). 질문에는 키 이름과 기본값 없는 사용 형태만 담는다.
- `tool.py`: `@tool("analyze_project")`. `tool_context("analyze_project")` 안에서만 AI가 허용된다(flow가 건다).
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 `__init__.py`의 공개 함수만 쓴다.
