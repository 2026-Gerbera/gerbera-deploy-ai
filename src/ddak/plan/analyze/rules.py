"""환경 키 추출·이름 기반 분류(규칙). 값은 읽지도 담지도 않는다. AI를 import하지 않는다."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

_NAME = r"[A-Z][A-Z0-9_]{0,63}"
_EXAMPLE_LINE = re.compile(rf"^\s*(?:export\s+)?({_NAME})\s*=")  # 값 쪽은 캡처하지 않는다
_SOURCE_USE = re.compile(rf"os\.(?:environ\[|environ\.get\(|getenv\()\s*['\"]({_NAME})['\"]")
_SECRET = re.compile(
    r"SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|_KEY$|^DATABASE_URL$"
)
_PLAIN = re.compile(
    r"^(?:"
    r"DEBUG|PORT|HOST|LOG_LEVEL|ENV|TZ|WORKERS|"
    r"APP_ENV|APP_BASE_URL|MIGRATE_MODE|DATABASE_URL_MIGRATOR|"
    r"PROXY_FIX_X_FOR|PROXY_FIX_X_PROTO|"
    r"RELEASE_ID|SOURCE_SHA"
    r")$"
    r"|_(?:ENABLED|SECURE|PORT|HOST|LEVEL)$"
)
_SKIP_DIRS = {"node_modules", "__pycache__"}
_MAX_BYTES = 1_000_000

Verdict = Literal["secret", "plain"] | None


def classify(name: str) -> Verdict:
    """secret 패턴이 plain 패턴보다 먼저다. None이면 애매(Jev에게 묻는다)."""
    if _SECRET.search(name):
        return "secret"
    return "plain" if _PLAIN.search(name) else None


def example_keys(path: Path) -> list[str]:
    """env_example의 `NAME=` 줄에서 이름만 뽑는다."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [m.group(1) for line in text.splitlines() if (m := _EXAMPLE_LINE.match(line))]


def _py_files(root: Path, rel: str) -> Iterator[Path]:
    base = root / rel
    if base.is_file():
        candidates = [base] if base.suffix == ".py" else []
    else:
        candidates = sorted(base.rglob("*.py")) if base.is_dir() else []
    for p in candidates:
        parts = p.relative_to(root).parts
        if p.is_symlink() or any(x.startswith(".") or x in _SKIP_DIRS for x in parts):
            continue
        if p.stat().st_size <= _MAX_BYTES:
            yield p


def source_keys(root: Path, rel: str) -> list[tuple[str, str, str]]:
    """(이름, 소스 상대경로, 사용 형태) 목록. 사용 형태는 기본값 인자를 버린 `os.getenv("X")`."""
    out: list[tuple[str, str, str]] = []
    for p in _py_files(root, rel):
        text = p.read_text(encoding="utf-8", errors="replace")
        relpath = p.relative_to(root).as_posix()
        out += [
            (m.group(1), relpath, f'os.getenv("{m.group(1)}")') for m in _SOURCE_USE.finditer(text)
        ]
    return out
