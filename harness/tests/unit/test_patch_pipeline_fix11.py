"""source=fixture: 탐지→제안→검사→승인→승인 트리 빌드 및 성공 원장."""

from dataclasses import replace
from pathlib import Path

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.plan_facts import EnvKey, Facts
from ddak.core.patch_ledger import file_diff
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.snapshots import digest_bytes
from ddak.executor.engine import RunStatus
from ddak.plan.patch.pipeline import prepare_patch
from tests.unit import test_deployment_service as support

rig = support.rig


@pytest.mark.anyio
async def test_hardcoded_change_reaches_approval_and_patched_build(rig):
    service, source, calls = rig
    old = b'import os\nSECRET_KEY = "' + b'dev"\n'
    fixed = b'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    (source / "app.py").write_bytes(old)
    facts = Facts(
        project="demo",
        mode=RunMode.UPDATE,
        target="local",
        tiers=("was",),
        changed={"local": {"was": True}},
        has_dockerfile={"was": True},
        source_snapshot_hash=digest_bytes(old),
        facts_hash=digest_bytes(old),
        code_patch=True,
        patch_targets=scan_patch_targets(source, ["app.py"]),
    )
    seen = []

    def propose(root, targets, ctx):
        seen.extend(t.model_dump() for t in targets)
        return (
            file_diff("app.py", old, fixed),
            (EnvKey(name="SECRET_KEY", kind="secret"),),
            "fixture",
        )

    result = prepare_patch(
        source,
        facts,
        RunContext("run-fix11", project="demo"),
        previous={},
        runs_root=service.root / "runs",
        proposer=propose,
        scanner=lambda src, patch: None,
    )
    assert seen[0]["pattern_id"] == "secret_key" and "dev" not in str(seen)
    plan = support.plan("run-fix11", patch=True)
    ctx = RunContext(
        plan.run_id,
        project="demo",
        toggles=plan.toggles,
        project_settings={"code_patch": True},
        required_env_keys=("SECRET_KEY",),
    )
    rid = service.prepare(plan, ctx, source, patch=result.patch, patch_meta=result.meta)
    view = service.approval_view(rid)
    assert view["patch_meta"]["passed"] is True
    assert view["patch_meta"]["gitleaks"] == "fixture"
    assert view["subjects"]["patch"] == digest_bytes(result.patch)
    assert view["missing_env_keys"] == [] and not calls.contexts
    service.approve(rid, approver="fixture-operator")
    service.start(rid)
    assert (await service.wait(rid)).status is RunStatus.SUCCEEDED
    build = next(ctx for name, ctx in calls.contexts if name == "build")
    assert (Path(build.build_source) / "app.py").read_bytes() == fixed
    assert (source / "app.py").read_bytes() == old
    release = service.get_release(rid)
    assert release["patch_ledger"]["app.py"]["result_sha256"] == digest_bytes(fixed)
    previous = {k: v["current"] for k, v in service.store.environments("demo").items()}
    off = prepare_patch(
        source,
        facts.model_copy(update={"code_patch": False}),
        replace(ctx, toggles={"code_patch": False}),
        previous=previous,
        runs_root=service.root / "runs",
        scanner=lambda src, patch: None,
    )
    assert off.patch == result.patch and off.meta["reuse"]


@pytest.mark.parametrize("mode", ["unavailable", "invalid", "no_targets"])
def test_proposal_failure_keeps_source_with_warning(rig, mode):
    from ddak.core.contracts.errors import DdakToolError, ErrorCode

    service, source, _ = rig
    old = b'SECRET_KEY = "' + b'dev"\n'
    (source / "app.py").write_bytes(old)
    facts = Facts(
        project="demo",
        mode=RunMode.UPDATE,
        target="local",
        tiers=("was",),
        changed={"local": {"was": True}},
        has_dockerfile={"was": True},
        source_snapshot_hash=digest_bytes(old),
        facts_hash=digest_bytes(old),
        code_patch=True,
        patch_targets=scan_patch_targets(source, ["app.py"]),
    )

    def propose(*args):
        if mode == "unavailable":
            raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "fixture")
        if mode == "no_targets":
            return b"", (), "fixture"
        return b"invalid fixture diff", (), "fixture"

    out = prepare_patch(
        source,
        facts,
        RunContext("run-fix11", project="demo"),
        previous={},
        runs_root=service.root / "runs",
        proposer=propose,
        scanner=lambda *_: None,
    )
    assert out.patch is None and out.env_keys == ()
    assert bool(out.warnings) == (mode != "no_targets")
    assert (source / "app.py").read_bytes() == old
