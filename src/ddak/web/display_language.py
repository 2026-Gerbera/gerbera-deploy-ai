"""저장된 생성문을 바꾸지 않는 표시 언어 보조 함수."""

from __future__ import annotations

import re

from jinja2 import pass_context

from ddak.web.narrative import SCENARIOS, short_summary, wording
from ddak.web.translations_ja import translate


def generated_in_korean(source: str, texts: str | list[str] | tuple[str, ...]) -> bool:
    """생성 출처의 실제 표시 문구만 검사한다. 설정·기술 JSON은 추측하지 않는다."""
    if source not in {"live", "cache", "replay"}:
        return False
    values = (texts,) if isinstance(texts, str) else texts
    return any(
        isinstance(text, str) and re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣᄀ-ᇿ]", text) for text in values
    )


@pass_context
def display_short_summary(context, text: str | None) -> str:
    """기존 요약 파싱을 따르되 JA 표시에서 삽입하는 이름만 일본어로 선택한다."""
    if context.get("ui_language") != "ja":
        return short_summary(text)
    text = str(text or "")
    if '{"' in text or "{'" in text:
        return translate("배포 기록을 확인했습니다.")
    for code, name in sorted(SCENARIOS.items(), key=lambda item: -len(item[0])):
        text = text.replace(code, translate(name))
    text = re.sub(
        r"(?:verify|deploy|build|prepare)\.[A-Za-z0-9_.-]+",
        lambda match: translate(wording(match[0])["name"]),
        text,
    )
    text = re.sub(r"\b(?:SUCCEEDED|FAILED_[A-Z_]+|DONE|RUNNING|null|None|error_code)\b", "", text)
    text = re.sub(r"sha256:[a-f0-9]+|\b[a-f0-9]{12,64}\b", "", text)
    text = re.sub(r"\b[A-Z][A-Z_]{3,}\b", "", text)
    text = re.sub(r"\b[A-Za-z_]+\.[A-Za-z0-9_.]+\b", translate("확인 작업"), text)
    text = re.sub(r"\s+", " ", text).strip(" ·:,-()")
    return text or translate("배포 기록을 확인했습니다.")
