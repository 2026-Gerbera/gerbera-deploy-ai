"""source=fixture: 수정11 패치/설정과 수정12 분석/스모크/시간 기록의 통합 회귀."""

from __future__ import annotations

import socket
import subprocess
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from ddak.core.ai.providers.jev import JevAnswer
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_config import DeployConfig
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import By, RunMode, Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Planner
from ddak.core.contracts.plan_draft import PlanDraft, StepDecision
from ddak.core.contracts.plan_facts import EnvKey, Facts, FileMeta
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput, AnalyzeProjectOutput
from ddak.core.contracts.tools.generate_plan import GeneratePlanOutput
from ddak.core.contracts.tools.validate_plan import ValidatePlanInput
from ddak.core.runtime import tool_context
from ddak.core.smoke import V2_BOX_MARK
from ddak.core.snapshots import file_manifest
from ddak.plan import flow
from ddak.plan.analyze import analyze_project, rules
from ddak.plan.intake import FetchPolicy
from ddak.plan.intake.fetch import Checkout
from ddak.plan.patch.pipeline import PatchPreparation
from ddak.plan.validate import validate_plan


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("통합 회귀에서 외부 프로세스/네트워크 호출은 금지된다")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


class FixtureJev:
    name = "fixture"
    model = "int1112"
    source = Source.FIXTURE

    def __init__(self, source=Source.FIXTURE):
        self.source = source
        self.calls = []

    def ask(self, *, state, questions):
        self.calls.append((state, questions))
        return [JevAnswer(id=q.id, probability=0.1) for q in questions]


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("source", [Source.FIXTURE, Source.REPLAY, Source.CACHE])
def test_wrapper_required_optional_patch_and_provenance(tmp_path, source):
    root = tmp_path / "source"
    write(
        root,
        "was/config.py",
        "import os as operating\n"
        "from os import environ as env, getenv as get\n"
        "def required(name):\n    return env[name]\n"
        "def optional(default, *, key):\n    return get(key, default)\n"
        "def mixed(name):\n    return get(name) or env[name]\n"
        "def store(name):\n    env[name] = 'fixture'\n"
        "def nested(name):\n    def inner():\n        return get(name)\n    return name\n"
        'required("SECRET_KEY")\n'
        'required("RELEASE_ID")\n'
        'optional("never-copy-default", key="CUSTOM_SETTING")\n'
        'optional("never-copy-default", key="API_TOKEN")\n'
        'mixed("DB_HOST")\n'
        'store("WRITE_TOKEN")\n'
        'nested("NESTED_TOKEN")\n'
        'app.secret_key = "' + "dev" + '"\n',
    )
    write(root, "was/index.html", V2_BOX_MARK)
    client = FixtureJev(source)
    inp = AnalyzeProjectInput(
        run_id="run-analysis",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="local"),
        source_dir="source",
        changed={"local": {"was": True}},
        changed_paths=("was/config.py",),
    )
    ctx = RunContext("run-analysis", deploy_config={"tiers": {"was": {"paths": ["was"]}}})
    with tool_context("analyze_project", inp.run_id):
        output = analyze_project(inp, ctx, root=tmp_path, jev_client=client)
    output = AnalyzeProjectOutput.model_validate_json(output.model_dump_json())
    keys = {key.name: key for key in output.env_keys}
    assert {name: key.required for name, key in keys.items()} == {
        "SECRET_KEY": True,
        "RELEASE_ID": False,
        "CUSTOM_SETTING": False,
        "API_TOKEN": False,
        "DB_HOST": True,
    }
    assert keys["CUSTOM_SETTING"].by is By.AI
    assert keys["CUSTOM_SETTING"].source is output.source is source
    assert keys["CUSTOM_SETTING"].provider == "fixture"
    assert keys["CUSTOM_SETTING"].model == "int1112"
    assert all(key.tier == "was" and key.is_new for key in output.env_keys)
    assert output.smoke_groups == ("v2",)
    assert [(t.pattern_id, t.is_new) for t in output.patch_targets] == [("secret_key", True)]
    assert "never-copy-default" not in str(client.calls) + output.model_dump_json()


def test_smoke_uses_current_tier_source_and_scan_exclusions(tmp_path, monkeypatch):
    cfg = DeployConfig(tiers={"was": {"paths": ["was"]}})
    for name in ("outside/index.html", "was/instance/index.html", "was/.hidden/index.html"):
        write(tmp_path, name, V2_BOX_MARK)
    real_open = Path.open

    def checked_open(path, *args, **kwargs):
        assert "instance" not in path.parts and ".hidden" not in path.parts
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    assert rules.smoke_groups(cfg, tmp_path) == ()
    write(tmp_path, "was/index.jinja2", V2_BOX_MARK)
    assert rules.smoke_groups(cfg, tmp_path) == ("v2",)
    write(tmp_path, "was/index.jinja2", "<p>v1</p>")
    assert rules.smoke_groups(cfg, tmp_path) == ()


def test_wrapper_defaults_are_not_keys_or_required_overrides(tmp_path):
    write(
        tmp_path,
        "config.py",
        "from os import environ as env\n"
        "def required(name, default=None):\n    return env[name]\n"
        "def optional(name, default='DEFAULT_VALUE_TOKEN'):\n    return env.get(name, default)\n"
        'required("SECRET_KEY", "POSITIONAL_DEFAULT_TOKEN")\n'
        'required(name="DB_HOST", default="KEYWORD_DEFAULT_TOKEN")\n'
        'optional("API_TOKEN")\n'
        'optional("LOG_LEVEL", "EXPLICIT_DEFAULT_TOKEN")\n',
    )
    reads = rules.source_key_reads(tmp_path, ".")
    assert {name: required for name, _, _, required in reads} == {
        "SECRET_KEY": True,
        "DB_HOST": True,
        "API_TOKEN": False,
        "LOG_LEVEL": False,
    }
    assert all("DEFAULT" not in snippet for _, _, snippet, _ in reads)
    assert rules.source_keys(tmp_path, ".") == [
        (name, path, snippet) for name, path, snippet, _ in reads
    ]


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("required_first", [False, True])
def test_wrapper_mixed_reads_keep_required_floor_in_plan(tmp_path, required, required_first):
    calls = ['optional("SECRET_KEY")\n']
    if required:
        calls.insert(0 if required_first else 1, 'required("SECRET_KEY")\n')
    write(
        tmp_path / "source",
        "was/config.py",
        "import os\n"
        "def optional(name):\n    return os.getenv(name)\n"
        "def required(name):\n    return os.environ[name]\n" + "".join(calls),
    )
    write(tmp_path / "source", "was/index.html", V2_BOX_MARK)
    ctx = RunContext("run-mixed", deploy_config={"tiers": {"was": {"paths": ["was"]}}})
    with tool_context("analyze_project", ctx.run_id):
        output = analyze_project(
            AnalyzeProjectInput(
                run_id=ctx.run_id,
                request=DeployRequest(
                    project="demo", repo_url="https://github.com/o/r", target="both"
                ),
                source_dir="source",
                changed={},
                changed_paths=("was/config.py",),
            ),
            ctx,
            root=tmp_path,
            jev_client=FixtureJev(),
        )
    assert len(output.env_keys) == 1 and output.env_keys[0].required is required
    facts = Facts(
        project="demo",
        mode=RunMode.UPDATE,
        target="both",
        tiers=output.tiers,
        env_keys=output.env_keys,
        patch_targets=output.patch_targets,
        smoke_groups=output.smoke_groups,
        changed={"local": {"was": False}, "cloud": {"was": False}},
        db_initialized={"local": True, "cloud": True},
        source_snapshot_hash="sha256:" + "a" * 64,
        facts_hash="sha256:" + "a" * 64,
    )
    if not required:
        # 필수 키가 없으면 두 환경 모두 바뀐 것이 없다: 계획 대신 배포할 변경 없음으로 끝난다.
        with pytest.raises(DdakToolError) as error:
            validate_plan(ValidatePlanInput(run_id=ctx.run_id, facts=facts), ctx)
        assert error.value.code is ErrorCode.PRECONDITION_FAILED
        return
    plan = validate_plan(ValidatePlanInput(run_id=ctx.run_id, facts=facts), ctx)
    for env in ("local", "cloud"):
        section = getattr(plan.deploy, env)
        ids = {step.id for step in section.steps}
        assert (f"deploy.config.{env}" in ids) is required
        assert (f"deploy.was.{env}" in ids) is required
        smoke = next(step for step in section.steps if step.tool == "smoke_test")
        assert smoke.params["scenarios"] == ["base", "v2"]
        assert "fact:smoke_groups" in smoke.evidence
    assert ("deploy.secrets.cloud" in {step.id for step in plan.deploy.cloud.steps}) is required


@pytest.mark.parametrize("target", ["local", "cloud", "both"])
@pytest.mark.parametrize("patched", [False, True])
def test_flow_context_patch_smoke_previous_and_stage_records(tmp_path, target, patched):
    policy = FetchPolicy(root=tmp_path / "sources")
    manifests = {}
    seen = []
    events = []

    def fetcher(url, ref, *, run_id, project, policy):
        root = policy.root / run_id
        write(root, "deploy.yaml", "tiers:\n  was:\n    paths: [was]\nmigrations_dir: migrations\n")
        write(root, "was/index.html", V2_BOX_MARK)
        write(root, "was/config.py", 'import os\nos.getenv("CUSTOM_SETTING")\nSECRET_KEY = "dev"\n')
        write(root, "migrations/001_init.sql", "select 1;\n")
        manifests.update(
            {name: FileMeta.model_validate(meta) for name, meta in file_manifest(root).items()}
        )
        return Checkout(root, "a" * 40)

    def previous(_):
        if target == "both":
            return {"local": manifests, "cloud": manifests}
        other = "cloud" if target == "local" else "local"
        return {target: manifests, other: None}

    def prepare(source, facts, ctx):
        seen.append((source, facts, ctx))
        return PatchPreparation(
            patch=b"fixture patch" if patched else None,
            meta={"source": "fixture"} if patched else None,
            env_keys=(EnvKey(name="SECRET_KEY", kind="secret", tier="was"),) if patched else (),
            warnings=("fixture patch warning",),
        )

    source_context = RunContext(
        "run-context",
        project="demo",
        targets="onprem" if target == "local" else target,
        trigger="auto",
        ref="refs/heads/prod",
        source_sha="b" * 40,
        build_backend="local",
        image_repository="example/demo",
        project_settings={"version": 7, "code_patch": patched},
        required_env_keys=("PREEXISTING_KEY",),
        source_checks={"source": "fixture"},
        preparation_warnings=["fixture source warning"],
        cloud_domain="example.test",
        platform={"local_build": {"source": "fixture"}},
    )
    plan_deployment = partial(
        flow.plan_deployment,
        DeployRequest(
            project="demo", repo_url="https://github.com/o/r", target=target, code_patch=patched
        ),
        run_id="run-flow",
        settings=Settings(ai_retries=0),
        previous_manifests=previous,
        fetch_policy=policy,
        fetcher=fetcher,
        jev_client=FixtureJev(Source.REPLAY),
        source_context=source_context,
        patch_preparer=prepare,
        record_stage=lambda *args: events.append(args),
    )
    if not patched:
        # 바뀐 것도 패치도 없으면 선택 환경이 모두 빠진다: 재지시 없이 배포할 변경 없음으로 끝낸다.
        with pytest.raises(DdakToolError) as error:
            plan_deployment()
        assert error.value.code is ErrorCode.PRECONDITION_FAILED
        assert error.value.message == "배포할 변경 없음"
        assert [(name, status) for name, _, status in events][-2:] == [
            ("plan", "succeeded"),
            ("validate", "failed"),
        ]
        before = seen[0][1]
        assert not any(changed for tiers in before.changed.values() for changed in tiers.values())
        return
    bundle = plan_deployment()
    source, before, context = seen[0]
    selected = {"local", "cloud"} if target == "both" else {target}
    assert set(before.changed) == set(before.db_initialized) == selected
    assert before.new_migrations == ()
    assert not any(changed for tiers in before.changed.values() for changed in tiers.values())
    assert before.patch_targets and not before.patch_targets[0].is_new
    assert before.smoke_groups == bundle.facts.smoke_groups == ("v2",)
    assert bundle.facts.patch_targets == before.patch_targets
    assert bundle.facts.facts_hash == before.facts_hash
    assert source == bundle.source
    assert bundle.patch == (b"fixture patch" if patched else None)
    assert bundle.patch_meta == ({"source": "fixture"} if patched else None)
    assert context.run_id == "run-flow" and context.source_sha == "a" * 40
    for field in (
        "targets",
        "trigger",
        "ref",
        "build_backend",
        "image_repository",
        "project_settings",
        "required_env_keys",
        "source_checks",
        "cloud_domain",
        "platform",
    ):
        assert (
            getattr(context, field)
            == getattr(bundle.context, field)
            == getattr(source_context, field)
        )
    assert bundle.context.preparation_warnings == [
        "fixture source warning",
        "fixture patch warning",
    ]
    assert source_context.preparation_warnings == ["fixture source warning"]
    assert all(
        changed is patched for tiers in bundle.facts.changed.values() for changed in tiers.values()
    )
    assert bundle.facts.infra_inputs_changed is patched
    assert bundle.plan.planner.source is Source.REPLAY
    assert (
        next(k for k in bundle.facts.env_keys if k.name == "CUSTOM_SETTING").source is Source.REPLAY
    )
    # 런타임 주입 뒤 같은 Facts로 재조립해도 시나리오와 판정 근거가 보존돼야 한다.
    roundtrip = Facts.model_validate_json(bundle.facts.model_dump_json())
    runtime_plan = validate_plan(
        ValidatePlanInput(run_id="run-flow", facts=roundtrip),
        replace(bundle.context, required_env_keys=("SECRET_KEY",) if patched else ()),
    )
    for plan in (bundle.plan, runtime_plan):
        for env in selected:
            section = getattr(plan.deploy, env)
            smoke = next(step for step in section.steps if step.tool == "smoke_test")
            assert smoke.params == {"scenarios": ["base", "v2"]}
            assert smoke.evidence == ["catalog:mandatory", "fact:smoke_groups"]
            config = [step for step in section.steps if step.id == f"deploy.config.{env}"]
            assert bool(config) is patched
            if patched:
                assert config[0].params["keys"] == ["SECRET_KEY"]
    assert [event[0] for event in events] == ["intake", "detect", "analyze", "plan", "validate"]
    assert all(
        isinstance(ms, int) and ms >= 0 and status == "succeeded" for _, ms, status in events
    )


@pytest.mark.parametrize("failure", ["intake", "analyze", "plan", "validate"])
def test_stage_failure_is_recorded_and_checkout_cleaned(tmp_path, monkeypatch, failure):
    policy = FetchPolicy(root=tmp_path / "sources")
    events = []

    def fail(*args, **kwargs):
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "fixture-private-error")

    def fetcher(url, ref, *, run_id, project, policy):
        root = policy.root / run_id
        write(root, "deploy.yaml", "tiers:\n  was:\n    paths: [was]\n")
        write(root, "was/app.py", "pass\n")
        return Checkout(root, "a" * 40)

    if failure != "intake":
        monkeypatch.setattr(
            flow,
            {"analyze": "analyze_project", "plan": "generate_plan", "validate": "validate_plan"}[
                failure
            ],
            fail,
        )
    with pytest.raises(DdakToolError):
        flow.plan_deployment(
            DeployRequest(project="demo", repo_url="https://github.com/o/r", target="local"),
            run_id="run-fail",
            settings=Settings(ai_retries=0),
            previous_manifests=lambda _: {},
            fetch_policy=policy,
            fetcher=fail if failure == "intake" else fetcher,
            jev_client=FixtureJev(),
            record_stage=lambda *args: events.append(args),
        )
    assert events[-1][0] == failure and events[-1][2] == "failed"
    assert all(isinstance(ms, int) and ms >= 0 for _, ms, _ in events)
    assert "fixture-private-error" not in str(events)
    assert not (policy.root / "run-fail").exists()


def test_retry_and_rule_fallback_keep_separate_stage_records(tmp_path, monkeypatch):
    events = []
    feedback = []

    def fetcher(url, ref, *, run_id, project, policy):
        root = policy.root / run_id
        write(root, "deploy.yaml", "tiers:\n  was:\n    paths: [was]\n")
        write(root, "was/index.html", V2_BOX_MARK)
        return Checkout(root, "a" * 40)

    def invalid(inp, ctx, **kwargs):
        feedback.append(inp.feedback)
        return GeneratePlanOutput(
            draft=PlanDraft(
                decisions=(
                    StepDecision(id="deploy.unknown.local", include=True, reason="fixture"),
                ),
                planner=Planner(by=By.AI, provider="fixture", source=Source.FIXTURE),
            )
        )

    monkeypatch.setattr(flow, "generate_plan", invalid)
    bundle = flow.plan_deployment(
        DeployRequest(project="demo", repo_url="https://github.com/o/r", target="local"),
        run_id="run-retry",
        settings=Settings(ai_retries=0),
        strict_ai_check=True,
        previous_manifests=lambda _: {},
        fetch_policy=FetchPolicy(root=tmp_path / "sources"),
        fetcher=fetcher,
        jev_client=FixtureJev(),
        record_stage=lambda *args: events.append(args),
    )
    assert len(feedback) == 2 and not feedback[0] and feedback[1]
    assert [(name, status) for name, _, status in events][-5:] == [
        ("plan", "succeeded"),
        ("validate", "failed"),
        ("plan", "succeeded"),
        ("validate", "failed"),
        ("validate", "succeeded"),
    ]
    assert bundle.plan.planner.fallback
    smoke = next(step for step in bundle.plan.deploy.local.steps if step.tool == "smoke_test")
    assert smoke.params["scenarios"] == ["base", "v2"]
    assert "fact:smoke_groups" in smoke.evidence


@pytest.mark.parametrize(
    "guard", ["not value or not value.strip()", "value is None", "None == value"]
)
def test_guarded_getenv_wrapper_remains_required_in_analysis(tmp_path, guard):

    root = tmp_path / "guarded"
    write(
        root,
        "config.py",
        "import os\n"
        "def required(name):\n"
        "    value = os.environ.get(name)\n"
        f"    if {guard}:\n"
        "        raise ValueError('missing')\n"
        "    return value\n"
        "def optional(name):\n"
        "    return os.environ.get(name)\n"
        "required('APP_BASE_URL')\noptional('MIGRATE_MODE')\n",
    )
    reads = {name: required for name, _, _, required in rules.source_key_reads(root, ".")}
    assert reads == {"APP_BASE_URL": True, "MIGRATE_MODE": False}
    ctx = RunContext("run-guarded", deploy_config={"tiers": {"was": {"paths": ["."]}}})
    with tool_context("analyze_project", ctx.run_id):
        output = analyze_project(
            AnalyzeProjectInput(
                run_id=ctx.run_id,
                request=DeployRequest(
                    project="demo", repo_url="https://github.com/o/r", target="local"
                ),
                source_dir="guarded",
                changed={"local": {"was": True}},
                changed_paths=("config.py",),
            ),
            ctx,
            root=tmp_path,
            jev_client=FixtureJev(),
        )
    facts = Facts(
        project="demo",
        mode=RunMode.UPDATE,
        target="local",
        tiers=output.tiers,
        env_keys=output.env_keys,
        changed={"local": {"was": True}},
        db_initialized={"local": True},
        source_snapshot_hash="sha256:" + "a" * 64,
        facts_hash="sha256:" + "a" * 64,
    )
    plan = validate_plan(ValidatePlanInput(run_id=ctx.run_id, facts=facts), ctx)
    config = next(step for step in plan.deploy.local.steps if step.id == "deploy.config.local")
    assert "APP_BASE_URL" in config.params["keys"]
    assert "MIGRATE_MODE" not in config.params["keys"]


@pytest.mark.parametrize(
    "guard_body",
    [
        "return 'default'",
        "if external_flag:\n            raise ValueError('missing')",
        "if not strict:\n            return 'fallback'\n        raise ValueError('missing')",
    ],
)
def test_guarded_wrapper_optional_fallback_is_not_promoted(tmp_path, guard_body):

    root = tmp_path / "optional"
    write(
        root,
        "config.py",
        "import os\ndef optional(name):\n"
        "    value = os.getenv(name)\n"
        "    if value is None:\n"
        f"        {guard_body}\n"
        "    return value\noptional('MIGRATE_MODE')\n",
    )
    assert rules.source_key_reads(root, ".")[0][-1] is False


@pytest.mark.parametrize(
    "read", ["os.getenv(name, 'fallback')", "os.environ.get(name, 'fallback')"]
)
def test_guarded_wrapper_with_default_remains_optional(tmp_path, read):
    write(
        tmp_path,
        "config.py",
        f"import os\ndef optional(name):\n    value = {read}\n"
        "    if not value:\n        raise ValueError('empty')\n"
        "    return value\noptional('OPTIONAL_SETTING')\n",
    )
    assert rules.source_key_reads(tmp_path, ".")[0][-1] is False


def test_empty_string_guard_does_not_reject_missing_key(tmp_path):
    write(
        tmp_path,
        "config.py",
        "import os\ndef optional(name):\n    value = os.getenv(name)\n"
        "    if value == '':\n        raise ValueError('empty')\n"
        "    return value\noptional('OPTIONAL_SETTING')\n",
    )
    assert rules.source_key_reads(tmp_path, ".")[0][-1] is False
