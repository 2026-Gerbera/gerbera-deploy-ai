"""비밀값 가리기. 로그, SSE 이벤트, run 로그, AI 입력, collect_diagnostics 출력, 에러 메시지에
공통 적용한다. "AI는 비밀값을 보지 않는다"를 코드로 강제하는 지점이다(TDD 필수 영역).

devpi-guardian의 privacy.py 개념만 참고해 새로 작성했다(코드 복사 없음).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
MAX_LEN = 4096

# 코드 속 비밀 이름 판정. 환경 키 분류 규칙(plan/analyze/rules.py)과 같은 단어를 쓴다.
# 단어는 대소문자 무시, PRIVATE와 이름 끝 _KEY는 대문자 설정 키만 본다(cache_key 같은 일반
# 변수는 가리지 않는다). TOKENS(input_tokens 등)는 아래 key=value 규칙처럼 제외한다.
_SECRET_WORD = re.compile(
    r"secret|password|passwd|token(?!s)|credential|api_?key|access_?key", re.IGNORECASE
)
_SECRET_UPPER = re.compile(r"PRIVATE|_KEY$")
# 따옴표로 감싼 키 이름. JSON 안에 다시 담긴 코드(\"...\")도 같은 따옴표 짝으로 본다.
_QUOTED_NAME = r"(?P<q>\\?[\"'])(?P<name>[A-Za-z0-9_.-]{1,128})(?P=q)"
# 문자열 리터럴 값(한 줄). f-string은 계산 값이라 가리지 않는다(접두사 f 제외).
_LITERAL = (
    r"(?P<value>(?:[rRbBuU]{1,2})?"
    r"(?:\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
    r"|\\\"(?:(?!\\\")[^\n])*\\\"|\\'(?:(?!\\')[^\n])*\\'))"
)


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_WORD.search(name) or _SECRET_UPPER.search(name))


def _mask_literal(match: re.Match[str]) -> str:
    """비밀 이름의 문자열 값만 따옴표를 남기고 가린다. 빈 값과 일반 키는 그대로 둔다."""
    value = match.group("value")
    body = value.lstrip("rRbBuU")
    prefix = value[: len(value) - len(body)]  # b"..."·r"..."의 접두사는 그대로 둔다
    quote = body[:2] if body.startswith("\\") else body[:1]
    if not _is_secret_name(match.group("name")) or len(body) <= 2 * len(quote):
        return match.group(0)
    return f"{match.group('head')}{prefix}{quote}{REDACTED}{quote}"


_Replacement = str | Callable[[re.Match[str]], str]

# (패턴, 치환). 순서가 중요하다: 블록 -> URL 자격증명 -> 헤더 -> ARN -> key=value -> 토큰 모양.
_RULES: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    # PEM 개인키 블록
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # URL 자격증명: scheme://user:password@host -> scheme://user:[REDACTED]@host
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+):[^/\s@]+@", re.IGNORECASE), rf"\1:{REDACTED}@"),
    # Authorization 헤더와 Bearer 토큰
    (re.compile(r"\b(authorization\s*[:=]\s*)\S+(\s+\S+)?", re.IGNORECASE), rf"\1{REDACTED}"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), f"Bearer {REDACTED}"),
    # JSON 형식 "password": "..."
    (
        re.compile(
            r"(\"[A-Za-z0-9_-]*(?:password|passwd|secret|token(?!s)|api_?key|access_?key)"
            r"[A-Za-z0-9_-]*\"\s*:\s*)\"[^\"]*\"",
            re.IGNORECASE,
        ),
        rf'\1"{REDACTED}"',
    ),
    # 코드의 첨자 대입: app.config["SECRET_KEY"] = "...", os.environ['API_TOKEN'] = '...'
    (re.compile(rf"(?P<head>\[\s*{_QUOTED_NAME}\s*\]\s*=\s*){_LITERAL}"), _mask_literal),
    # 비밀 이름 키의 기본값: os.environ.get("SECRET_KEY", "..."), getenv("X_TOKEN", "..."),
    # setdefault·pop·env(...)·getattr(obj, "SECRET_KEY", "...")와 default= 키워드
    (
        re.compile(
            r"(?P<head>\b(?:(?:get|getenv|setdefault|pop|env)\s*\("
            r"|getattr\s*\(\s*[A-Za-z_][A-Za-z0-9_.]*\s*,)"
            rf"\s*{_QUOTED_NAME}\s*,\s*(?:default\s*=\s*)?){_LITERAL}"
        ),
        _mask_literal,
    ),
    # ARN의 12자리 계정 ID(에러 메시지에 계정 ID를 남기지 않는다)
    (re.compile(r"\b(arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:)\d{12}(?=:)"), r"\1************"),
    # .env / key=value 형식: *_SECRET, *_TOKEN, *_PASSWORD, *_KEY, aws_secret_access_key 등.
    # 비밀 단어 뒤에는 "_단어" 하나만 허용한다(secretsmanager: 같은 서비스 이름은 제외).
    (
        re.compile(
            r"\b([A-Za-z0-9_]*(?:secret|token(?!s)|password|passwd|pwd|api_?key|access_?key|_key)"
            r"(?:_[A-Za-z0-9]+)?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&;,]+)",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
    # AWS 액세스 키 ID
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Anthropic / OpenAI 형식 키
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    # Groq 키(생성·판단 두 경로에서 같은 공통 가림 적용)
    (re.compile(r"\bgsk_[A-Za-z0-9_-]{16,}"), REDACTED),
    # GitHub 토큰
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), REDACTED),
    # Docker Hub 개인·조직 액세스 토큰(✅ 9/30: 이미지 저장소 기본 Docker Hub)
    (re.compile(r"\bdckr_(?:pat|oat)_[A-Za-z0-9_-]{16,}"), REDACTED),
    # 💭 Slack(장부 30): 봇·사용자 토큰, 앱 토큰, Incoming Webhook URL
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"(https://hooks\.slack\.com/services/)[A-Za-z0-9/_-]+"), rf"\1{REDACTED}"),
)


def redact(text: str, *, max_len: int | None = MAX_LEN) -> str:
    """비밀값 모양을 가린다. max_len이 None이면 길이를 제한하지 않는다."""
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    if max_len is not None and len(out) > max_len:
        cut = len(out) - max_len
        out = f"{out[:max_len]}...[truncated {cut} chars]"
    return out


_SECRET_KEY = re.compile(
    r"(secret|token(?!s)|password|passwd|api_?key|access_?key|authorization|credential)",
    re.IGNORECASE,
)


def redact_obj(value: Any, *, max_len: int = MAX_LEN) -> Any:
    """dict/list 안의 문자열을 재귀적으로 가린다. 비밀스러운 키 이름의 값은 통째로 가린다."""
    if isinstance(value, str):
        return redact(value, max_len=max_len)
    if isinstance(value, Mapping):
        return {
            k: REDACTED
            if isinstance(k, str) and _SECRET_KEY.search(k) and v not in (None, "")
            else redact_obj(v, max_len=max_len)
            for k, v in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_obj(v, max_len=max_len) for v in value]
    return value
