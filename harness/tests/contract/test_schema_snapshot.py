"""스키마 스냅샷 테스트.

- 생성이 결정적인지(두 번 만들면 같은지) 확인한다.
- 커밋된 contracts/schemas/와 계약 모델·툴 카탈로그가 같은지 확인한다.
  계약을 바꿨으면 같은 PR에서 `make contracts-update`.
"""

from __future__ import annotations

import json

from tests.support import load_script

export = load_script("export_schemas")


def test_schema_generation_is_deterministic() -> None:
    first = export.expected_files()
    second = export.expected_files()
    assert first == second
    for name in ("ping.input.json", "events.json", "plan.json", "tool_catalog.json"):
        assert name in first


def test_ping_input_schema_is_strict() -> None:
    schema = json.loads(export.expected_files()["ping.input.json"])
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["run_id", "target"]


def test_tool_catalog_snapshot_has_40_plus_example() -> None:
    catalog = json.loads(export.expected_files()["tool_catalog.json"])
    assert sum(1 for t in catalog if t["canonical"]) == 40
    assert [t["name"] for t in catalog] == sorted(t["name"] for t in catalog)


def test_committed_schemas_match() -> None:
    drift = export.check()
    assert drift == [], "계약 스냅샷 드리프트: `make contracts-update` 후 커밋한다\n" + "\n".join(
        drift
    )
