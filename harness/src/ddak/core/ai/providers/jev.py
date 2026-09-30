"""Jev(TypeSafe AI, 판단 전용 모델) 클라이언트 자리. 담당 O2. ✅ 장부 8: 팀이 키 보유.

쓰는 곳: ① 플랜의 step 선택(필요 여부 예/아니오 묶음)과 환경 키 분류. 규칙이 바닥(필수·조건부)을
정하고 Jev는 추가·경고만 한다. ⑤ 원인 범주 분류는 선택. 실패·지연이면 규칙/Claude 대체 경로.
쓰지 않는 곳: 마이그레이션 파괴형 판정, 게이트·롤백 판단, 보고 심각도, 채팅 의도.

확인 필요(키로 실측):
- 엔드포인트 POST https://api.typesafe.ai/v1/systemone, 모델 jev-1.13.0 고정(jev-latest 쓰지 않음).
- Python SDK(typesafe-sdk)의 import 이름. SDK 기본값(타임아웃 10초 + 재시도 2회)을 2초·1회로 줄인다.
- 서울에서의 실제 추론 지연(추정 0.25~0.7초). 대체 기준 후보: 20회 p95 <= 1.5초.
보내는 것: 키 이름과 코드 줄만. 값은 절대 보내지 않는다. 질문은 영어가 가장 정확하다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"  # 확인 필요


class JevQuestion(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    kind: Literal["noul", "choice", "score"]  # 예/아니오 확률, 보기 중 하나, 단계 점수
    text: str = Field(max_length=2000)
    choices: tuple[str, ...] = ()


class JevAnswer(ContractModel):
    id: str
    probability: float | None = None  # noul
    choice: str | None = None  # choice
    confidence: float | None = None


class JevClient:
    """TODO(O2): SDK 또는 HTTP 호출 구현. 지금은 호출하지 않는다(규칙 대체 경로 확인용)."""

    name = "jev"

    def __init__(self, api_key: str | None, *, model: str, timeout_s: float) -> None:
        self._key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def ask(self, *, state: str, questions: Sequence[JevQuestion]) -> list[JevAnswer]:
        del state, questions
        raise NotImplementedError("Jev 호출 미구현(TODO(O2))")
