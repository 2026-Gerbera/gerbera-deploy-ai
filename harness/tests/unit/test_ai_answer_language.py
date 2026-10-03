"""언어 전달 회귀: AI·Git·네트워크 실행 없이 호출 직전 입력을 검증한다."""

from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest
from pydantic import ValidationError

from ddak.cloud.infra.tools.generate_infra import storage
from ddak.core.answer_language import request_language, with_answer_language
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.tools.answer_code_question import AnswerCodeQuestionInput
from ddak.core.contracts.tools.diagnose_parity_gap import DiagnoseParityGapInput
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput
from ddak.core.contracts.tools.post_report import PostReportInput, ReportFacts
from ddak.core.defaults import project_values
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.project_settings import ProjectSettings
from ddak.core.runtime import tool_context
from ddak.plan.analyze import code_question, infra_mapping
from ddak.plan.patch import generate as patch
from ddak.verify.diagnose import advisory
from ddak.verify.report import logic as report
from tests.unit.cloud.infra.test_generate_storage import context as storage_context

JA_LINE = (
    "自由記述のフィールドは日本語で書いてください。JSONのキー・列挙値・構造は変更しないでください。"
)
# 언어 연결 전 KO 프롬프트의 UTF-8 바이트 해시.
KO_HASHES = {
    "report": "e5bbf1d43695a8cc700043b03f5c038ebdc3096bc0b0f6719898b75889e08fa1",
    "storage": "e72f9dc38393586afe4299e6141d3f83b77c8ab696b70a15c040585a21b98dbf",
    "question": "f62bafb6945b83f49a7268e289857eb5fb0caa3f4e433eb84c5f193e68678a84",
    "diagnose": "22411b1fab3d49bc1515bd2e1b4f628391a97d20e98677ee3c49601e185b3ec8",
    "patch": "a2f175976876769139b138fc1ab51be483420552280fa51937147165689a4c94",
    "intents": "5d2d3827571ddc189072fb769142fdbc9cd85cbeec2ad608589e51270cac12d1",
}
MAPPING_KO = (
    "로컬 리소스 흔적에 대응하는 클라우드 서비스 후보를 제안한다. "
    "target은 s3/rds/elasticache/sqs/ses/none 중 하나다. "
    "evidence_id는 입력에 있는 것만 사용하고 reason은 200자 이하로 설명한다. "
    "값이나 코드를 추측하지 않는다. 자동 생성되지 않는 표시용 제안이다."
)


class Captured(BaseException):
    """AI 호출 직전 중단하며 제품의 Exception fallback에도 삼켜지지 않는다."""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("이 테스트에서는 외부 명령이나 네트워크를 실행하지 않는다")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings()))


@pytest.mark.parametrize("language", [None, "ko", "ja", "invalid"])
def test_helper_preserves_original_and_only_appends_one_line(language):
    original = "한국어 원문\r\n공백 유지  \n"
    expected = original + "\n" + JA_LINE if language == "ja" else original
    assert with_answer_language(original, language).encode() == expected.encode()
    assert with_answer_language(original) == original


def test_setting_defaults_validation_and_normalization():
    assert ProjectSettings().ai_answer_language == "ko"
    assert project_values({})["ai_answer_language"] == "ko"
    assert project_values({"ai_answer_language": "ja"})["ai_answer_language"] == "ja"
    for invalid in (None, "en", "JA", 1):
        with pytest.raises(ValidationError):
            ProjectSettings(ai_answer_language=invalid)


@pytest.mark.parametrize("site", [*KO_HASHES, "mapping"])
def test_six_generation_sites_preserve_ko_bytes_and_append_ja(monkeypatch, tmp_path, site):
    # 패치는 현재 intents 경로와 이전 줄 편집 경로 둘 다 포함한다.
    seen = []

    def capture(**kwargs):
        seen.append(kwargs)
        raise Captured

    for module in (report, advisory, patch, infra_mapping, code_question):
        monkeypatch.setattr(module, "call_ai", capture)
    (tmp_path / "app.py").write_text('SECRET_KEY = "fixture"\nclient.connect("localhost:6379")\n')
    directory = tmp_path / "bundle"
    directory.mkdir()
    for language in (None, "ko", "ja"):
        values = {} if language is None else {"ai_answer_language": language}
        ctx = RunContext("run-language", project_settings=values, toggles={"code_patch": True})
        # 백그라운드 run은 우연히 전파된 HTTP ContextVar를 참조하면 안 된다.
        token = request_language.set("ko" if language == "ja" else "ja")
        try:
            with pytest.raises(Captured), tool_context("patch_config", ctx.run_id):
                if site == "report":
                    report.post_report(
                        PostReportInput(run_id=ctx.run_id, facts=ReportFacts(status="SUCCEEDED")),
                        replace(ctx, adapter_mode=AdapterMode.REAL),
                    )
                elif site == "diagnose":
                    advisory.diagnose_parity_gap(
                        DiagnoseParityGapInput(run_id=ctx.run_id, reason="track_failed"), ctx
                    )
                elif site == "patch":
                    patch.propose_config_patch(tmp_path, ctx)
                elif site == "intents":
                    patch.propose_intents(tmp_path, scan_patch_targets(tmp_path, ()), ctx)
                elif site == "mapping":
                    options = {} if language is None else {"language": language}
                    infra_mapping.suggest_infra_mappings(tmp_path, settings=Settings(), **options)
                elif site == "question":
                    code_question.answer_code_question(
                        AnswerCodeQuestionInput(
                            run_id=ctx.run_id, question="q", commit="a" * 40, branch="prod"
                        ),
                        ctx,
                    )
                elif site == "storage":
                    storage.generate_storage(
                        GenerateInfraInput(
                            run_id="run-storage", directory=str(directory), layer="app"
                        ),
                        storage_context(**values),
                        ai=capture,
                    )
        finally:
            request_language.reset(token)
    assert len(seen) == 3
    original = seen[0]["instruction"]
    if site == "mapping":
        assert original == MAPPING_KO
    else:
        assert sha256(original.encode()).hexdigest() == KO_HASHES[site]
    assert seen[1]["instruction"].encode() == original.encode()
    assert seen[2]["instruction"] == original + "\n" + JA_LINE
    # 언어 지시 외 모델·버전·데이터·도구 구조는 같아야 한다.
    assert {k: v for k, v in seen[0].items() if k != "instruction"} == {
        k: v for k, v in seen[2].items() if k != "instruction"
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "requested,saved,expected",
    [(None, "ja", "ja"), ("ko", "ja", "ko"), ("ja", "ko", "ja"), (None, "ko", "ko")],
)
async def test_code_question_callback_carries_request_language_to_session(
    monkeypatch, requested, saved, expected
):
    from ddak import app
    from ddak.core import code_question as core_question

    values = {"repo_url": "https://example.test/app", "ai_answer_language": saved}
    repository = SimpleNamespace(git=Mock(return_value="a" * 40), git_bytes=Mock())
    service = SimpleNamespace(
        get_project_settings=lambda project: values,
        connect_repository=lambda ctx: repository,
        registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(fn=code_question.answer_code_question)
        ),
        onboarding=None,
    )
    monkeypatch.setattr(
        core_question,
        "collect_context",
        lambda *a: SimpleNamespace(
            files=(), paths=(), total_bytes=0, skipped_secret=0, truncated=False
        ),
    )
    captured = Mock(
        return_value=SimpleNamespace(
            value=code_question._Answer(answer="fixture", sources=[]),
            source=Source.FIXTURE,
            usage=None,
        )
    )
    monkeypatch.setattr(code_question, "call_ai", captured)
    qa = app._code_question_service(service, Settings())
    before = request_language.get()
    token = request_language.set(requested)
    try:
        result = await anyio.to_thread.run_sync(qa.ask, "demo", "q")
    finally:
        request_language.reset(token)
    assert result["answer"] == "fixture"
    instruction = captured.call_args.kwargs["instruction"]
    assert instruction == code_question._PROMPT + ("\n" + JA_LINE if expected == "ja" else "")
    assert values["ai_answer_language"] == saved
    assert request_language.get() == before
    assert code_question._SESSION.get() is None


@pytest.mark.parametrize("language", ["ko", "ja"])
def test_saved_language_survives_unrelated_settings_update_and_snapshot(tmp_path, language):
    from ddak.core.store import Store

    store = Store(tmp_path / "store.sqlite")
    first = store.save_project_settings(
        "demo", {"ai_answer_language": language}, updated_by="operator", expected_version=0
    )
    store.save_project_settings(
        "demo", {"code_patch": False}, updated_by="operator", expected_version=first["version"]
    )
    saved = store.project_settings("demo")
    values = project_values(saved)
    ctx = RunContext("run-language", project_settings=values, trigger="auto")
    assert ctx.to_json_dict()["project_settings"]["ai_answer_language"] == language
    assert values["code_patch"] is False


@pytest.mark.parametrize("language", ["ko", "ja"])
def test_flow_keeps_saved_language_through_analysis(monkeypatch, tmp_path, language):
    from ddak.core.contracts.deploy_request import DeployRequest
    from ddak.core.contracts.release import SnapshotBinding
    from ddak.core.contracts.tools.analyze_project import AnalyzeProjectOutput
    from ddak.plan import flow
    from ddak.plan.intake import FetchPolicy

    digest = "sha256:" + "a" * 64
    snapshot = SnapshotBinding(source_snapshot_hash=digest, build_snapshot_hash=digest)
    (tmp_path / "checkout").mkdir()
    monkeypatch.setattr(
        flow,
        "receive_deploy_request",
        lambda *a, **k: SimpleNamespace(
            source_dir="checkout", deploy_config={}, commit="a" * 40, snapshot=snapshot
        ),
    )
    monkeypatch.setattr(
        flow,
        "detect_changed_tiers",
        lambda *a, **k: SimpleNamespace(
            changed={"local": {"was": True}},
            new_migrations=(),
            modified_migrations=(),
            changed_paths=(),
            facts_hash=digest,
        ),
    )
    monkeypatch.setattr(
        flow,
        "analyze_project",
        lambda *a, **k: AnalyzeProjectOutput(
            tiers=("was",), env_keys=(), has_dockerfile={"was": True}, infra_inputs_changed=False
        ),
    )
    mapping = Mock(return_value=[{"reason": "fixture"}])
    monkeypatch.setattr(flow, "suggest_infra_mappings", mapping)
    contexts = []

    def prepared(source, facts, ctx):
        contexts.append(ctx)
        raise Captured

    token = request_language.set("ja" if language == "ko" else "ko")
    try:
        with pytest.raises(Captured):
            flow.plan_deployment(
                DeployRequest(project="demo", repo_url="https://example.test/app", target="local"),
                run_id="run-language",
                settings=Settings(),
                previous_manifests=lambda project: {"local": None},
                fetch_policy=FetchPolicy(root=tmp_path),
                patch_preparer=prepared,
                source_context=RunContext(
                    "run-language",
                    trigger="auto",
                    project_settings={"ai_answer_language": language},
                ),
            )
    finally:
        request_language.reset(token)
    assert mapping.call_args.kwargs["language"] == language
    assert contexts[0].trigger == "auto"
    assert contexts[0].project_settings["ai_answer_language"] == language
    assert contexts[0].project_settings["_infra_mapping_suggestions"] == [{"reason": "fixture"}]
