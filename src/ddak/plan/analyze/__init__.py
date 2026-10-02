"""plan/analyze: 프로젝트 분석·환경 키 분류(규칙 + Jev). 담당 김준석(O2).

공개: analyze_project(툴 본체). AI는 ddak.core.ai 의 ask_jev 로만 부른다(허용 디렉토리, 계약 2).
"""

from __future__ import annotations

from ddak.plan.analyze.logic import analyze_project

__all__ = ["analyze_project"]
