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


@pytest.fixture(autouse=True)
def _isolated_build_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    """빌드 캐시(cloud/build/image.py)는 테스트마다 임시 폴더. 실제 var/build-cache를 안 쓴다."""
    monkeypatch.setenv("DDAK_BUILD_CACHE_DIR", str(tmp_path_factory.mktemp("build-cache")))
