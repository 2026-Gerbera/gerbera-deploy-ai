"""integrations/slack 빈 구현: import되고 호출하면 NotImplementedError를 낸다."""

from __future__ import annotations

import pytest

from ddak.integrations.slack import post_webhook


def test_slack_skeleton_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="integrations/slack 미구현: 담당 미정"):
        post_webhook("test")
