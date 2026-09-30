"""골든 패스 v2 plan.json 예시(fixtures/plans/golden_v2_update.json)가 계약과 맞는지.

설계 문서 02 파이프라인과 계획 6-4의 예시와 같은 내용이다.
문서·계약·카탈로그가 어긋나면 여기서 깨진다.
- Plan 모델로 파싱되고(wait_for는 배열, skipped에도 layer, 보고는 verify.report + run=finally)
- 실행기 계획 모양 검사(W1·W3·교착·finally 규칙)를 통과하고
- step의 tool·layer·target이 레지스트리 카탈로그와 맞고
- params에 도메인·주소·이미지·ARN 같은 금지 키가 없다(V3 일부. 전체 검사는 validate_plan).
- 데모 시크릿 장면(✅ 9/30)의 인프라 step이 G1 뒤에 있고 시크릿 값 쓰기·태스크 정의 등록보다 앞선다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ddak.core.contracts.enums import Effect, Target
from ddak.core.contracts.plan import Plan
from ddak.core.registry import canonical_names, spec_for
from ddak.executor.engine import check_signals

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "plans" / "golden_v2_update.json"
# 설계 문서 02 7-2 V3의 금지 키(어느 깊이든)
FORBIDDEN_KEYS = {
    "domain",
    "host",
    "hostname",
    "url",
    "uri",
    "endpoint",
    "zone",
    "fqdn",
    "registry",
    "image",
    "digest",
    "arn",
    "role",
    "profile",
    "account",
    "command",
    "cmd",
    "script",
    "buildspec",
    "value",
    "password",
    "token",
    "secret_value",
}


def _plan() -> Plan:
    return Plan.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def test_golden_plan_parses_and_passes_shape_checks() -> None:
    plan = _plan()
    check_signals(plan)
    assert plan.toggles == {"code_patch": True}  # 최신 데모 기준 ON, 일반 기본값은 OFF
    assert [s.run for s in plan.verify.steps] == [None, "finally"]


def test_golden_plan_matches_catalog() -> None:
    plan = _plan()
    sections = (plan.build, plan.deploy.local, plan.deploy.cloud, plan.verify)
    names = canonical_names()
    for section in sections:
        for item in (*section.steps, *section.skipped):
            assert item.tool in names, item.id
            spec = spec_for(item.tool)
            assert item.layer is spec.layer, f"{item.id}: 층이 카탈로그와 다르다"
            if item.target is not None:
                assert item.target in spec.targets, f"{item.id}: 대상이 카탈로그에 없다"


def test_golden_plan_cloud_state_changes_wait_for_local_verified() -> None:
    plan = _plan()
    for step in plan.deploy.cloud.steps:
        assert step.target is Target.CLOUD
        if step.effect is Effect.STATE_CHANGE:
            assert "local_verified" in step.wait_for, step.id


def test_golden_plan_secret_scene_infra_step() -> None:
    # 데모 시크릿 장면(✅ 9/30): v2의 새 SECRET_KEY 때문에 AI Terraform이 앱 층에 시크릿과 실행 역할
    # 읽기 권한을 추가한다. plan에 in-place update(정책 변경)가 있어 G1 뒤(V23, 💭), 값 쓰기와
    # 태스크 정의 등록은 그 뒤다(시크릿 ARN은 인프라 출력에서 코드가 읽음).
    plan = _plan()
    ids = [s.id for s in plan.deploy.cloud.steps]
    infra = plan.deploy.cloud.steps[ids.index("deploy.infra.cloud")]
    assert infra.tool == "apply_infra"
    assert infra.effect is Effect.STATE_CHANGE
    assert infra.wait_for == ["local_verified"]
    assert infra.signal == "infra_ready"
    order = [
        ids.index("deploy.infra.cloud"),
        ids.index("deploy.secrets.cloud"),
        ids.index("deploy.config.cloud"),
        ids.index("deploy.app.cloud"),
    ]
    assert order == sorted(order)


def test_golden_plan_has_no_forbidden_param_keys() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for section in (
        data["build"],
        data["deploy"]["local"],
        data["deploy"]["cloud"],
        data["verify"],
    ):
        for step in section["steps"]:
            assert not (_keys(step.get("params", {})) & FORBIDDEN_KEYS), step["id"]
