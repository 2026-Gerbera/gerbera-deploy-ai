"""run별 제품 도구 경로. 병렬 프로젝트가 전역 PATH를 덮어쓰지 않는다."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_scanner: ContextVar[str] = ContextVar("approved_scanner", default="gitleaks")


def scanner_binary() -> str:
    return _scanner.get()


@contextmanager
def managed_tools(directory: str | Path | None):
    candidate = Path(directory) / "gitleaks" if directory else None
    token = _scanner.set(str(candidate) if candidate is not None else "gitleaks")
    try:
        yield
    finally:
        _scanner.reset(token)
