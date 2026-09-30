"""레지스트리 카탈로그 계약 테스트.

배치·플래그·층을 바꾸면 이 스냅샷도 같은 계약 변경 PR에서 고친다.
모듈 배치는 9/30 레지스트리 트리 고려안(💭): plan / infra / ci / cd / verify + core + ops.
"""

from __future__ import annotations

import re
from collections import Counter

from ddak.core.contracts.enums import Layer, Module, Stage, Target, ToolKind
from ddak.core.registry import CATALOG, PING, ROLES, ai_tools, canonical_names, spec_for, tools_in

AI_TOOLS = {
    "analyze_project",
    "generate_plan",
    "patch_db_access",
    "patch_storage",
    "patch_config",
    "diagnose_parity_gap",
    "post_report",
    "generate_infra",  # AI Terraform 초안(✅ 장부 21). 제안만, apply는 apply_infra(코드)
    "generate_dockerfile",  # AI Dockerfile 초안(✅ 9/30, Dockerfile이 없을 때만). 검사는 코드
}
DESTRUCTIVE = {
    "apply_infra",
    "sync_env_to_cloud",
    "prepare_db",
    "deploy_tier",
    "rollback_tier",
    "reset_demo_state",
    "cleanup",
}
REQUIRES_LOCK = {
    "apply_infra",
    "inject_env_config",
    "sync_env_to_cloud",
    "prepare_db",
    "prepare_storage",
    "ensure_tls",
    "deploy_tier",
    "rollback_tier",
}
# step 층 스냅샷(✅ 장부 5). 필수는 AI가 뺄 수 없고, 내장은 계획에 나오지 않는다.
MANDATORY = {
    "ensure_tls",
    "health_check",
    "smoke_test",
    "verify_tls",
    "compare_env_results",
    "post_report",
}
BUILTIN = {
    "acquire_deploy_lock",
    "push_image",  # 💭 빌드 뒤 digest 확인·기록(실행기 내장 단계에서 부름)
    "rollback_tier",
    "collect_diagnostics",
    "diagnose_parity_gap",
    "record_deploy_log",
    "request_approval",
    "stream_progress",
}
CONDITIONAL = {
    "apply_infra",  # 부트스트랩은 강제 포함, 개선 배포는 인프라 요구가 바뀔 때만
    "build_image",
    "inject_env_config",
    "sync_env_to_cloud",
    "prepare_db",
    "deploy_tier",
}
OPTIONAL = {"prepare_storage", "watch_post_deploy"}
# 사람 승인이 따로 필요한 툴. apply_infra는 plan 요약·IAM diff를 사람이 승인한 뒤에만(✅ 장부 23)
REQUIRES_APPROVAL = {
    "apply_infra",
    "patch_db_access",
    "patch_storage",
    "patch_config",
    "reset_demo_state",
    "cleanup",
}
# 인프라 툴(💭 이름·개수). 옛 ensure_infra(존재 확인만)를 대체한다.
INFRA_TOOLS = {"discover_existing", "generate_infra", "validate_infra", "plan_infra", "apply_infra"}
# CD 인터페이스 함수를 부르는 툴(✅ 9/30: 공통 인터페이스 + provider 모듈)
CD_INTERFACE_TOOLS = {
    "deploy_tier",
    "rollback_tier",
    "health_check",
    "prepare_db",
    "inject_env_config",
    "sync_env_to_cloud",
    "ensure_tls",
}


def _canonical() -> list:
    return [s for s in CATALOG if s.canonical]


def test_exactly_40_unique_canonical_tools() -> None:
    names = [s.name for s in _canonical()]
    assert len(names) == 40
    assert len(set(names)) == 40
    assert PING not in names
    assert canonical_names() == set(names)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)


def test_module_counts() -> None:
    counts = Counter(s.module for s in _canonical())
    assert counts == {
        Module.PLAN: 10,
        Module.INFRA: 5,
        Module.CI: 2,
        Module.CD: 9,
        Module.VERIFY: 8,
        Module.CORE: 3,
        Module.OPS: 3,
    }


def test_ai_tools_snapshot() -> None:
    # 채팅 의도 JSON을 AI 툴로 등록하면 10개가 된다(이름·위치 결정 필요). 그때 같은 PR에서 고친다.
    assert ai_tools() == AI_TOOLS


def test_execution_path_has_no_ai() -> None:
    # ③ 빌드, ④ 배포, 운영, 실행기 기능에는 AI가 없다(✅ 장부 4).
    assert not any(s.uses_ai for s in tools_in(Module.CI))
    assert not any(s.uses_ai for s in tools_in(Module.CD))
    assert not any(s.uses_ai for s in tools_in(Module.OPS))
    assert not any(s.uses_ai for s in CATALOG if s.kind is ToolKind.EXECUTOR_FN)
    assert not any(s.uses_ai for s in CATALOG if s.stage in (Stage.BUILD, Stage.DEPLOY))
    assert {s.module for s in CATALOG if s.uses_ai} <= {Module.PLAN, Module.INFRA, Module.VERIFY}


def test_flag_snapshots() -> None:
    assert {s.name for s in CATALOG if s.destructive} == DESTRUCTIVE
    assert {s.name for s in CATALOG if s.requires_lock} == REQUIRES_LOCK
    assert all(s.stage is Stage.DEPLOY for s in CATALOG if s.requires_lock)
    assert {s.name for s in CATALOG if s.requires_approval} == REQUIRES_APPROVAL


def test_layer_snapshots() -> None:
    by_layer = {layer: {s.name for s in _canonical() if s.layer is layer} for layer in Layer}
    assert by_layer[Layer.MANDATORY] == MANDATORY
    assert by_layer[Layer.BUILTIN] == BUILTIN
    assert by_layer[Layer.CONDITIONAL] == CONDITIONAL
    assert by_layer[Layer.OPTIONAL] == OPTIONAL
    # 계획을 만드는 쪽(①②)과 운영은 계획에 step으로 나오지 않는다.
    assert all(s.layer is Layer.OUTSIDE for s in tools_in(Module.PLAN))
    assert all(s.layer is Layer.OUTSIDE for s in tools_in(Module.OPS))
    # 인프라 제안·검사는 계획 밖, apply만 조건부 step(deploy.infra.cloud)이다.
    assert {s.name for s in tools_in(Module.INFRA) if s.layer is not Layer.OUTSIDE} == {
        "apply_infra"
    }


def test_tls_tools_are_cloud_only() -> None:
    # 클라우드 HTTPS 필수, 로컬 HTTPS는 ⏸ 보류(✅ 장부 17).
    assert spec_for("ensure_tls").targets == (Target.CLOUD,)
    assert spec_for("verify_tls").targets == (Target.CLOUD,)
    assert spec_for("sync_env_to_cloud").targets == (Target.CLOUD,)


def test_flags_are_consistent() -> None:
    for s in CATALOG:
        assert not (s.read_only and s.destructive), s.name
        assert s.owners, s.name
        assert set(s.owners.values()) <= set(ROLES), s.name
        assert set(s.owners) <= {"logic", "local", "cloud", "ui"}, s.name


def test_kinds() -> None:
    assert spec_for("call_ai").kind is ToolKind.LIBRARY
    assert spec_for("request_approval").kind is ToolKind.EXECUTOR_FN
    assert spec_for("stream_progress").kind is ToolKind.EXECUTOR_FN
    assert sum(s.kind is ToolKind.TOOL_FN for s in _canonical()) == 37


def test_validate_plan_is_deterministic() -> None:
    spec = spec_for("validate_plan")
    assert spec.uses_ai is False
    assert spec.module is Module.PLAN


def test_dockerfile_tools_split_generator_from_checker() -> None:
    # Dockerfile이 없으면 AI가 초안만 만들고(✅ 9/30), 정적 검사·빌드 확인은 코드가 한다.
    gen, val = spec_for("generate_dockerfile"), spec_for("validate_dockerfile")
    assert gen.uses_ai is True and val.uses_ai is False
    assert gen.module is val.module is Module.PLAN  # import-linter 계약 5가 둘을 분리
    assert gen.stage is Stage.PLAN and val.stage is Stage.VALIDATE
    assert gen.layer is val.layer is Layer.OUTSIDE  # 승인 전 ①②에서 돈다(plan.json step 아님)
    assert not (gen.destructive or val.destructive)


def test_ci_has_build_and_push_without_ai() -> None:
    assert {s.name for s in tools_in(Module.CI)} == {"build_image", "push_image"}
    assert spec_for("push_image").stage is Stage.BUILD


def test_cd_tools_call_the_common_interface() -> None:
    # CD = 공통 인터페이스 + provider 모듈(✅ 9/30). 인터페이스 함수를 부르는 툴은 모두 cd에 있다.
    assert {s.name for s in tools_in(Module.CD)} >= CD_INTERFACE_TOOLS
    assert spec_for("health_check").stage is Stage.VERIFY_REPORT  # 단계와 모듈은 1:1이 아님


def test_infra_tools_split_generator_from_checker_and_executor() -> None:
    # AI는 제안(HCL 초안)만 만들고, 검사·plan·apply는 코드가 한다(✅ 장부 4·21·23).
    assert {s.name for s in _canonical()} >= INFRA_TOOLS
    assert "ensure_infra" not in canonical_names()
    assert {n for n in INFRA_TOOLS if spec_for(n).uses_ai} == {"generate_infra"}
    for name in INFRA_TOOLS:
        spec = spec_for(name)
        assert spec.module is Module.INFRA, name
        assert spec.targets == (Target.CLOUD,), name  # AI Terraform은 클라우드만
        assert "C1" in spec.owners.values(), name
    for name in ("discover_existing", "generate_infra"):
        assert spec_for(name).stage is Stage.PLAN, name
    for name in ("validate_infra", "plan_infra"):
        assert spec_for(name).stage is Stage.VALIDATE, name
        assert spec_for(name).read_only, name


def test_apply_infra_needs_human_approval_and_has_no_ai() -> None:
    spec = spec_for("apply_infra")
    assert spec.module is Module.INFRA  # import-linter 계약 7: AI 관문·generate_infra import 금지
    assert spec.stage is Stage.DEPLOY
    assert spec.uses_ai is False
    assert spec.requires_approval and spec.requires_lock and spec.destructive
    assert spec.layer is Layer.CONDITIONAL
