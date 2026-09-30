"""관리 웹(C3 양서윤). 채팅·계획 카드·2열 진행(SSE)·결과·설정(도메인)·LLM 연결 카드.

💭 FastAPI + Jinja2 + HTMX, SSE는 FastAPI 내장 EventSourceResponse.
기능 우선, 디자인은 나중(✅ 장부 19). 템플릿·정적 파일은 이 패키지 안(templates/, static/)에 둔다.
htmx는 static에 벤더링한다(데모 중 CDN 의존 없음).
AI를 import하지 않는다(import-linter 계약 1). LLM 상태 함수는 ddak.app이 주입한다.
"""
