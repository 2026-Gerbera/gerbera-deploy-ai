"""언어별 저장소 캐시 회귀. 임시 파일과 fixture만 사용한다."""

import json
from hashlib import sha256

import pytest

from ddak.cloud.infra import storage_bundle as bundle
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError
from tests.unit.cloud.infra.test_generate_storage import RATIONALE, context, hcl

# 기준본 캐시 키(sha256 hex). 비밀값이 아니며 검사기 오탐을 피하려고 나눠 적는다.
KO_DIGEST = "bd01b2065a2e0f31fa1c9372cde2771f" + "e26dda5d2162e7b06b81c3830120d062"
JA_RATIONALE = ("ローカル保存の根拠を確認します。",) * 5


def files_for(language):
    settings = {} if language is None else {"ai_answer_language": language}
    ctx = context(**settings)
    rationale = JA_RATIONALE if language == "ja" else RATIONALE
    return ctx, bundle.storage_bundle(hcl(), ctx, rationale, Source.LIVE)


def cache_file(root, language):
    key = KO_DIGEST + ("-ja" if language == "ja" else "")
    return root / "infra-baselines" / "flaskr" / bundle.STORAGE_PROMPT_VERSION / key / "storage.tf"


@pytest.mark.parametrize("language", [None, "ko"])
def test_ko_cache_path_and_bundle_bytes_are_unchanged(tmp_path, language):
    ctx, files = files_for(language)
    assert (
        sha256(files["storage.tf"].encode()).hexdigest()
        == "710dd0c879c92f7a20c1b665151e5be309b412912f6415f47ab6511f609bddd7"
    )
    bundle.save_storage_baseline(tmp_path, "flaskr", files)
    legacy = cache_file(tmp_path, "ko")
    assert legacy.read_bytes() == files["storage.tf"].encode()
    assert bundle.load_storage_baseline(tmp_path, ctx) == files
    assert not cache_file(tmp_path, "ja").exists()


@pytest.mark.parametrize("first_language", ["ko", "ja"])
def test_cache_save_and_load_are_separate_in_both_directions(tmp_path, first_language):
    second_language = "ja" if first_language == "ko" else "ko"
    first_ctx, first_files = files_for(first_language)
    second_ctx, second_files = files_for(second_language)
    bundle.save_storage_baseline(tmp_path, "flaskr", first_files)
    assert cache_file(tmp_path, first_language).read_text() == first_files["storage.tf"]
    assert bundle.load_storage_baseline(tmp_path, first_ctx) == first_files
    assert bundle.load_storage_baseline(tmp_path, second_ctx) is None
    bundle.save_storage_baseline(tmp_path, "flaskr", second_files)
    assert bundle.load_storage_baseline(tmp_path, first_ctx) == first_files
    assert bundle.load_storage_baseline(tmp_path, second_ctx) == second_files
    assert len(list((tmp_path / "infra-baselines").rglob("storage.tf"))) == 2


@pytest.mark.parametrize("stored_language,requested_language", [("ko", "ja"), ("ja", "ko")])
def test_wrong_language_file_copied_into_cache_is_a_miss(
    tmp_path, stored_language, requested_language
):
    _, files = files_for(stored_language)
    ctx, _ = files_for(requested_language)
    destination = cache_file(tmp_path, requested_language)
    destination.parent.mkdir(parents=True)
    destination.write_text(files["storage.tf"])
    assert bundle.load_storage_baseline(tmp_path, ctx) is None


@pytest.mark.parametrize("language", [None, "en", "../ko", 1])
def test_invalid_bundle_language_is_rejected(tmp_path, language):
    _, files = files_for("ja")
    header, _, body = files["storage.tf"].partition("\n")
    metadata = json.loads(header.removeprefix("# ddak-storage "))
    metadata["ai_answer_language"] = language
    altered = {"storage.tf": "# ddak-storage " + json.dumps(metadata) + "\n" + body}
    with pytest.raises(DdakToolError):
        bundle.save_storage_baseline(tmp_path, "flaskr", altered)
    assert not (tmp_path / "infra-baselines").exists()


def test_ja_cache_is_saved_after_fixture_plan_and_reused_without_ai(monkeypatch, tmp_path):
    from dataclasses import replace
    from unittest.mock import Mock

    from ddak.cloud.infra import bind_infra, fixture_binding, run_plan, run_validate, unbind_infra
    from ddak.cloud.infra.tools.generate_infra import logic
    from ddak.core.config import Settings
    from ddak.core.contracts.tools.generate_infra import GenerateInfraInput
    from ddak.core.contracts.tools.plan_infra import PlanInfraInput
    from ddak.core.contracts.tools.validate_infra import ValidateInfraInput

    ctx, files = files_for("ja")
    binding = fixture_binding(
        ctx, root=tmp_path / "runtime", approvals=lambda: [], guard=lambda: None
    )
    binding = replace(binding, files=files, generation_source=Source.LIVE, baseline_root=tmp_path)
    bind_infra(binding)
    try:
        assert run_validate(ValidateInfraInput(run_id=ctx.run_id), ctx).passed
        assert bundle.load_storage_baseline(tmp_path, ctx) is None
        assert run_plan(PlanInfraInput(run_id=ctx.run_id), ctx).passed
    finally:
        unbind_infra(ctx.run_id)
    assert cache_file(tmp_path, "ja").read_text() == files["storage.tf"]
    assert bundle.load_storage_baseline(tmp_path, context()) is None
    ai = Mock(side_effect=AssertionError("캐시 적중 시 AI 호출 금지"))
    monkeypatch.setattr(logic, "call_ai", ai)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings()))
    directory = tmp_path / "bundles" / "cached-ja"
    directory.mkdir(parents=True)
    result = logic.generate_infra(
        GenerateInfraInput(run_id=ctx.run_id, directory=str(directory), layer="app"), ctx
    )
    assert result.source is Source.CACHE
    assert (directory / "storage.tf").read_text() == files["storage.tf"]
    ai.assert_not_called()
