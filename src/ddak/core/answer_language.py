"""AI 자유문장 출력 언어. 요청 언어와 저장된 run 언어는 호출부가 구분한다."""

from contextvars import ContextVar

request_language: ContextVar[str | None] = ContextVar("ddak_request_language", default=None)

_JA_INSTRUCTION = (
    "自由記述のフィールドは日本語で書いてください。JSONのキー・列挙値・構造は変更しないでください。"
)


def with_answer_language(prompt: str, language: str | None = "ko") -> str:
    """KO 원문은 그대로 반환하고 JA일 때만 마지막 지시 한 줄을 덧붙인다."""
    return prompt + "\n" + _JA_INSTRUCTION if language == "ja" else prompt
