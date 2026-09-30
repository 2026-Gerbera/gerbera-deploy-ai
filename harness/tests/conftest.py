"""tests/ 공용 픽스처. anyio_backend는 루트 conftest.py에 있다.

공용 함수는 conftest가 아니라 tests/support.py에 둔다(conftest는 직접 import하지 않는다).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import REPO_ROOT


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
