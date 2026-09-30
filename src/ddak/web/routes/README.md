# web/routes (담당: 양서윤)
- 할 일: FastAPI 라우터(채팅·계획 카드·진행 SSE·결과·설정). 기존 `web/app.py`의 동작은 유지하고 여기로 나눈다.
- 입출력 계약: `src/ddak/core/contracts`
- 툴 모듈·다른 디렉토리 안쪽 파일을 직접 import하지 않는다(실행은 DeploymentService, 툴은 레지스트리 이름으로만).
- AI 호출 금지(LLM 상태 함수는 ddak.app이 주입한다).
