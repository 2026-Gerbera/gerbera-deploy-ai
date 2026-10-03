"""source=fixture: 패치 승인·단계 기록·v2 smoke 선택이 통합 뒤 함께 작동한다."""

import json
import stat

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target, ToolKind
from ddak.core.contracts.tools.smoke_test import SmokeTestInput, SmokeTestOutput
from ddak.core.registry import Registry
from ddak.core.smoke import V2_BOX_MARK
from ddak.verify.smoke.fake import FakeFlaskr, FakeSmokeAdapter
from ddak.verify.smoke.logic import V2_BOX_TEXT, Response, run_smoke
from tests.e2e import test_ai_patch_fix11 as support
from tests.e2e.test_ai_patch_fix11 import FixtureInput, deploy
from tests.unit.core.test_app_repository import git
from tests.unit.core.test_candidate import operator_identity  # noqa: F401

rig = support.rig


@pytest.mark.anyio
async def test_patch_approval_smoke_and_timing_survive_roundtrip(rig):
    scenarios_seen = []

    class V2Client(FakeFlaskr):
        def request(self, method, path, form, timeout):
            response = super().request(method, path, form, timeout)
            if method == "GET" and path == "/":
                return Response(
                    response.status,
                    response.headers,
                    response.body + page.read_text(),
                )
            return response

    class Adapter(FakeSmokeAdapter):
        def client(self, ctx):
            return V2Client(ctx.run_id)

    async def smoke(inp: FixtureInput, ctx: RunContext) -> SmokeTestOutput:
        rig.calls.append(("smoke_test", ctx))
        scenarios_seen.append(list(inp.scenarios))
        return run_smoke(
            Adapter(Target.LOCAL),
            SmokeTestInput(run_id=ctx.run_id, target="local", scenarios=inp.scenarios),
            ctx,
        )

    original_registry = rig.service.registry
    registry = Registry(original_registry.specs)
    for spec in registry.specs:
        if spec.kind is ToolKind.TOOL_FN:
            registry.tool(spec.name)(
                smoke if spec.name == "smoke_test" else original_registry.get(spec.name).fn
            )
    rig.service.registry = registry
    page = rig.developer / "was/index.html"
    try:
        for version, groups in (("v1", []), ("v2", ["v2"]), ("v1-again", [])):
            page.write_text(
                f"<section {V2_BOX_MARK}>{V2_BOX_TEXT}<svg></svg></section>"
                if groups
                else "<p>v1</p>"
            )
            git(rig.developer, "add", "was/index.html")
            git(rig.developer, "commit", "-m", "Update smoke fixture " + version)
            git(rig.developer, "push", "origin", "prod")
            rig.source_sha = git(rig.developer, "rev-parse", "HEAD")
            rid, _ = await deploy(rig)
            view = rig.service.approval_view(rid)
            assert view["patch"] is None and view["patch_meta"]["passed"]
            assert view["decision_basis"]["facts"]["smoke_groups"] == groups
            assert any(
                "fact:smoke_groups" in (step["evidence"] or [])
                for step in view["decision_basis"]["steps"]
            )
            assert scenarios_seen[-1] == ["base", *groups]
            directory = rig.service.root / "runs" / rid
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert stat.S_IMODE((directory / "approved.patch").stat().st_mode) == 0o600
            events = rig.service.events(rid)
            assert [e["seq"] for e in events] == list(range(len(events)))
            timed = [e for e in events if e["type"] == "stage.finished"]
            assert [e["preparation_stage"] for e in timed] == [
                "intake",
                "detect",
                "analyze",
                "plan",
                "validate",
                "source",
                "prepare",
            ]
            assert all(e["elapsed_ms"] >= 0 and e["status"] == "succeeded" for e in timed)
            # 저장 화면을 조회하는 과거 실행 경로에서도 원문은 다시 노출하지 않는다.
            saved = json.loads((directory / "approval-view.json").read_text())
            assert saved["patch"] is None
        assert scenarios_seen == [["base"], ["base", "v2"], ["base"]]
        assert len(rig.provider.requests) == 1  # 같은 원본 파일의 승인 패치는 재사용한다.
    finally:
        await rig.service.shutdown()
