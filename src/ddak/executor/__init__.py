"""실행기(O1 정준우). plan.json 순서대로 레지스트리 함수를 직접 호출한다. AI 없음.

- engine: Executor(트랙 병렬 + wait_for/signal 대기 지점 + 트랙별 규칙 롤백), 계획 모양 검사
- events: stream_progress 이벤트 버스(-> JSONL -> SSE)
실행기는 툴 모듈과 ddak.core.ai를 import하지 않는다(import-linter 계약 1·4).
"""
