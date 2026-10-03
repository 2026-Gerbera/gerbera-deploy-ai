# plan/analyze (담당: 정준우/O1, 2026-10-03 O2 승계)
- 할 일: tier·Dockerfile 유무·환경 키(plain/secret) 분석(`analyze_project`). 입출력: `core/contracts/tools/analyze_project.py`.
- `rules.py`: env_example `NAME=` 줄과 소스 `os.environ/os.getenv` 정규식과 같은 파일의 환경 키 wrapper AST로 키 추출(값·기본값은 수집하지 않음), 이름 패턴 분류. AI 없음.
- `logic.py`: 규칙이 바닥. 애매한 키만 `ask_jev`(noul 질문)에 묻는다. 애매한 키는 실제 판단 결과에 따라 plain 또는 secret이 된다. 규칙으로 정한 secret/plain은 질문 대상이 아니므로 AI가 뒤집지 않는다.
  Jev 불가면 애매한 키는 secret(보수). 질문에는 키 이름과 기본값 없는 사용 형태만 담는다.
- `tool.py`: `@tool("analyze_project")`. `tool_context("analyze_project")` 안에서만 AI가 허용된다(flow가 건다).
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 `__init__.py`의 공개 함수만 쓴다.

- 스모크 그룹은 현재 배포 소스의 선언 tier 템플릿 표식으로 정한다. v2 표식이 사라진 v1 재배포는 추가 그룹이 빈 tuple이다. AI가 그룹을 정하지 않는다.
- wrapper 추론은 같은 파일의 top-level 함수가 매개변수를 `environ[]`, `environ.get`, `getenv`로 읽고, 그 함수 호출에서 해당 인자에 문자열 literal을 주는 경우만 지원한다. os/from os 별칭과 positional/keyword 인자를 지원한다. 동적 이름·외부 모듈 wrapper·간접 wrapper chain은 추측하지 않는다. AI 질문에는 값 없는 정규화 사용 형태만 들어간다.
