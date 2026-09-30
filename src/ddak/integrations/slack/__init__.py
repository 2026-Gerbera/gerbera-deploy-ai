"""integrations/slack: Slack Incoming Webhook 알림(P0-라이트, 단방향). 담당 미정.

공개 함수: post_webhook. 웹훅 URL은 비밀로 다룬다(설정에서 읽고 로그·AI·화면에 넣지 않는다).
AI import 금지. 빈 구현이다.
"""

from __future__ import annotations

_TODO = "integrations/slack 미구현: 담당 미정"


def post_webhook(text: str) -> None:
    """보고 요약 한 건을 보낸다. 입력: redact된 짧은 텍스트. 출력: 없음(실패는 예외)."""
    raise NotImplementedError(_TODO)
