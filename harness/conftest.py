"""저장소 전체(tests/)에 적용되는 pytest 설정.

비동기 테스트는 anyio 플러그인으로 돌린다(pytest-asyncio 금지). 백엔드는 asyncio 하나만 쓴다.
anyio 기본값은 설치된 모든 백엔드를 돌리므로 여기서 고정한다.
"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
