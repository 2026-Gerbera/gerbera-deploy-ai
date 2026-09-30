"""tests/ 공용 도우미. pytest pythonpath=["."] 설정으로 `from tests.support import ...`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str) -> ModuleType:
    """패키지가 아닌 파이썬 파일(scripts/*.py, .claude/hooks/*.py)을 모듈로 불러온다."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"불러올 수 없다: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_script(name: str) -> ModuleType:
    return load_module(REPO_ROOT / "scripts" / f"{name}.py", f"_script_{name}")
