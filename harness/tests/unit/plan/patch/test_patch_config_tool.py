"""patch_config 툴 입출력: 원본 경로, 상태별 출력 칸, 실행기 prepare 메타 모양, 레지스트리 등록."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import (
    ApprovedPatch,
    PatchConfigInput,
    PatchConfigOutput,
)
from ddak.core.runtime import tool_context
from ddak.core.snapshots import digest_bytes
from ddak.executor.approval_meta import encode_meta
from ddak.plan.patch import patch_config

CONFIG = "flaskr/config.py"
# 샘플 앱 config.load() 모양(10/3 실제 Claude 시험의 후보 앱과 같다)
ORIGINAL = """import os


def load():
    return {
        "APP_BASE_URL": "http://localhost:5000",
        "SECRET_KEY": "dev",
    }
"""
EDITS = [
    {
        "path": CONFIG,
        "start": 6,
        "end": 7,
        "lines": [
            '        "APP_BASE_URL": os.environ["APP_BASE_URL"],',
            '        "SECRET_KEY": os.environ["SECRET_KEY"],',
        ],
    }
]
CFG = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="m-claude")
ON = RunContext("run-1", toggles={"code_patch": True})


def reply(edits: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "edits": edits,
            "reason": "주소와 서명 키를 환경변수에서 읽게 바꿨다",
            "env_vars": ["APP_BASE_URL", "SECRET_KEY"],
        }
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    def complete(self, req: AIRequest) -> AIResponse:
        if not self.replies:
            raise AssertionError("AI를 부르면 안 된다")
        return AIResponse(text=self.replies.pop(0), source=Source.REPLAY)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "snap" / "flaskr").mkdir(parents=True)
    (tmp_path / "snap" / CONFIG).write_text(ORIGINAL, encoding="utf-8")
    return tmp_path


def run(root: Path, provider: FakeProvider, **kw: object) -> PatchConfigOutput:
    inp = PatchConfigInput(run_id="run-1", source_dir="snap", **kw)  # type: ignore[arg-type]
    with tool_context("patch_config", "run-1"):
        return patch_config(inp, ON, root=root, provider=provider, settings=CFG)


def test_proposed_output_is_ready_for_executor_prepare(root: Path) -> None:
    out = run(root, FakeProvider(reply(EDITS)))
    assert out.status == "proposed" and out.passed and out.patch is not None
    assert out.patch_sha256 == digest_bytes(out.patch.encode("utf-8"))
    assert out.meta is not None and out.meta.reuse is False and out.meta.source is Source.REPLAY
    encode_meta(out.meta.model_dump(mode="json"))  # 실행기 승인 메타 검사를 통과한다
    assert out.targets == {CONFIG: ["local_address", "secret_key"]}
    assert out.env_vars == ["APP_BASE_URL", "SECRET_KEY"]
    assert out.attempts == 1 and out.source is Source.REPLAY and out.violations == []
    PatchConfigOutput.model_validate_json(out.model_dump_json())  # JSON 왕복


def test_previous_patch_is_reused_without_ai(root: Path) -> None:
    first = run(root, FakeProvider(reply(EDITS)))
    assert first.patch is not None
    previous = ApprovedPatch(patch=first.patch, reason="이전 승인", source=Source.LIVE)
    out = run(root, FakeProvider(), previous=previous)
    assert out.status == "reused" and out.passed and out.attempts == 0
    assert out.meta is not None and out.meta.reuse is True and out.source is None


def test_rejected_output_is_not_passed(root: Path) -> None:
    bad = [{"path": CONFIG, "start": 7, "end": 7, "lines": ['        "SECRET_KEY": "other",']}]
    out = run(root, FakeProvider(reply(bad), reply(bad)))
    assert out.status == "rejected" and not out.passed and out.meta is None
    assert out.violations and all(v.code for v in out.violations)


@pytest.mark.parametrize("source_dir", ["../snap", "/abs/snap", "missing"])
def test_source_dir_must_be_an_existing_relative_snapshot(root: Path, source_dir: str) -> None:
    inp = PatchConfigInput(run_id="run-1", source_dir=source_dir)
    with pytest.raises(DdakToolError) as caught:
        patch_config(inp, ON, root=root, provider=FakeProvider(), settings=CFG)
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED


def test_patch_config_is_registered() -> None:
    assert "patch_config" in load_tools().registered()
