"""수정12 v1/v2 판단 재생. 저장 응답만 사용하며 인프라·Git·LLM을 호출하지 않는다."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.ai.providers.claude import ClaudeJevClient
from ddak.core.ai.providers.replay import ReplayProvider
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.plan_facts import Facts
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.contracts.tools.generate_plan import GeneratePlanInput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.runtime import tool_context
from ddak.core.smoke import V2_BOX_MARK
from ddak.plan.analyze import analyze_project
from ddak.plan.planner import generate_plan
from ddak.plan.validate import validate_plan

ROOT = Path(__file__).resolve().parents[1]
REPLAYS = ROOT / "fixtures" / "ai_replay"


class FixtureWriter:
    """사람이 검토할 합성 응답을 저장하는 전용 옵션. 실제 판단 결과를 가장하지 않는다."""

    name = "replay"

    def __init__(self, replay: ReplayProvider):
        self.replay = replay

    def complete(self, req: AIRequest) -> AIResponse:
        data = json.loads(
            req.user.removeprefix("<untrusted_data>\n").removesuffix("\n</untrusted_data>")
        )
        output = {"answers": [{"id": q["id"], "probability": 0.2} for q in data["questions"]]}
        self.replay.save(req, output)
        return self.replay.complete(req)


def replay_case(case: str, *, write_fixtures: bool = False) -> dict:
    load_tools()
    replay = ReplayProvider(REPLAYS)
    provider = FixtureWriter(replay) if write_fixtures else replay
    judgment = ClaudeJevClient(model="offline-fixture", provider=provider)
    cfg = {"tiers": {"was": {"paths": ["was"]}}}
    ctx = RunContext("run-replay", project="demo", deploy_config=cfg)
    request = DeployRequest(
        project="demo", repo_url="https://github.com/example/demo", target="local"
    )
    with tempfile.TemporaryDirectory(prefix="o2-replay-") as tmp:
        root = Path(tmp)
        source = root / "source" / "was"
        source.mkdir(parents=True)
        (source / "app.py").write_text('import os\nMODE = os.getenv("APP_MODE")\n')
        (source / "index.html").write_text(
            f"<section {V2_BOX_MARK}></section>" if case == "v2" else "<p>v1</p>"
        )
        with tool_context("analyze_project", ctx.run_id):
            analyzed = analyze_project(
                AnalyzeProjectInput(
                    run_id=ctx.run_id,
                    request=request,
                    source_dir="source",
                    changed={"local": {"was": True}},
                ),
                ctx,
                root=root,
                jev_client=judgment,
            )
    facts = Facts(
        project="demo",
        mode=RunMode.UPDATE,
        target="local",
        tiers=analyzed.tiers,
        changed={"local": {"was": True}},
        db_initialized={"local": True},
        env_keys=analyzed.env_keys,
        smoke_groups=analyzed.smoke_groups,
        source_snapshot_hash="sha256:" + "a" * 64,
        facts_hash="sha256:" + "a" * 64,
    )
    with tool_context("generate_plan", ctx.run_id):
        planned = generate_plan(
            GeneratePlanInput(run_id=ctx.run_id, facts=facts),
            ctx,
            jev_client=judgment,
            provider=replay,
            settings=Settings(ai_retries=0),
        )
    plan = validate_plan(
        ValidatePlanInput(run_id=ctx.run_id, facts=facts, draft=planned.draft), ctx
    )
    return {
        "case": case,
        "source": planned.source.value,
        "analyze_source": analyzed.source.value,
        "smoke_groups": facts.smoke_groups,
        "planner": plan.planner.model_dump(mode="json"),
        "scenarios": [
            s.params["scenarios"] for s in plan.deploy.local.steps if s.tool == "smoke_test"
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("v1", "v2"), default="v1")
    parser.add_argument(
        "--write-fixtures", action="store_true", help="합성 fixture 저장; 외부 호출 없음"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            replay_case(args.case, write_fixtures=args.write_fixtures), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
