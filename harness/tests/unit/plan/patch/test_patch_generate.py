"""patch_config 제안 로직: 대상 찾기, 재사용, AI 제안 적용·검사·재시도, 가림."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import LLMBackend, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.runtime import tool_context
from ddak.executor.approval_meta import encode_meta
from ddak.plan.patch import PatchEdit, PreviousPatch, find_targets, propose_config_patch
from ddak.plan.patch.check import build_patch
from ddak.plan.patch.generate import apply_edits

APP = "flaskr/__init__.py"
ORIGINAL = """from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="dev",
        SESSION_COOKIE_SECURE=False,
    )
    app.config["APP_BASE_URL"] = "http://localhost:5000"
    return app
"""
GOOD_EDITS = [
    {"path": APP, "start": 1, "end": 0, "lines": ["import os", ""]},
    {"path": APP, "start": 7, "end": 7, "lines": ['        SECRET_KEY=os.environ["SECRET_KEY"],']},
    {
        "path": APP,
        "start": 8,
        "end": 8,
        "lines": [
            "        SESSION_COOKIE_SECURE="
            'os.environ.get("SESSION_COOKIE_SECURE", "false") == "true",'
        ],
    },
    {
        "path": APP,
        "start": 10,
        "end": 10,
        "lines": ['    app.config["APP_BASE_URL"] = os.environ["APP_BASE_URL"]'],
    },
]
REASON = "서명 키·쿠키 Secure·기본 주소를 환경변수에서 읽게 바꿨다"
CHECKED_REASON = "환경변수 전환: 쿠키 Secure·개발 주소·서명 키"
CFG = Settings(ai_retries=0, llm_backend=LLMBackend.API, llm_model="m-claude")
ON = RunContext("run-1", toggles={"code_patch": True})
# 가림 확인용 가짜 값. 비밀값 스캐너에 걸리지 않게 이어 붙여 만든다(AGENTS 4절)
FAKE_SECRET = "fake-" + "signing-" + "value-123"


def reply(edits: list[dict[str, object]], env_vars: list[str] | None = None) -> str:
    return json.dumps(
        {"edits": edits, "reason": REASON, "env_vars": env_vars or ["SECRET_KEY", "APP_BASE_URL"]}
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[AIRequest] = []

    def complete(self, req: AIRequest) -> AIResponse:
        self.seen.append(req)
        if not self.replies:
            raise AssertionError("AI를 부르면 안 된다")
        return AIResponse(text=self.replies.pop(0), source=Source.REPLAY)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "flaskr").mkdir(parents=True)
    (root / APP).write_text(ORIGINAL, encoding="utf-8")
    (root / "flaskr" / "blog.py").write_text(
        "# localhost 문구는 주석일 뿐\nx = 1\n", encoding="utf-8"
    )
    (root / "migrations").mkdir()
    (root / "migrations" / "env.py").write_text('URL = "localhost"\n', encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text('SECRET_KEY = "test"\n', encoding="utf-8")
    return root


def propose(source: Path, provider: FakeProvider, **kw: object):
    with tool_context("patch_config", "run-1"):
        return propose_config_patch(source, ON, provider=provider, settings=CFG, **kw)  # type: ignore[arg-type]


def test_toggle_off_is_refused_before_anything(source: Path) -> None:
    with pytest.raises(DdakToolError) as caught:
        propose_config_patch(source, RunContext("run-1"), provider=FakeProvider())
    assert caught.value.code is ErrorCode.TOGGLE_OFF


def test_find_targets_skips_comments_tests_and_migrations(source: Path) -> None:
    assert find_targets(source) == {APP: ["cookie_secure", "local_address", "secret_key"]}


def test_no_targets_means_no_patch_and_no_ai_call(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    out = propose(tmp_path, FakeProvider())
    assert out.status == "no_targets" and out.patch is None and out.attempts == 0


def test_ai_edits_become_a_checked_patch_for_the_executor(source: Path) -> None:
    provider = FakeProvider(
        reply(GOOD_EDITS, ["SECRET_KEY", "APP_BASE_URL", "lower", "UNUSED_KEY"])
    )
    out = propose(source, provider)
    assert out.status == "proposed"
    assert out.check is not None and out.check.passed, out.check
    assert out.patch is not None and out.patch.startswith(b"--- a/flaskr/__init__.py\n")
    assert b"+import os\n" in out.patch and b'-        SECRET_KEY="dev",\n' in out.patch
    assert out.meta == {"reason": CHECKED_REASON, "reuse": False, "source": "replay"}
    encode_meta(out.meta)  # 실행기 prepare(patch_meta=)가 받는 모양이다
    assert out.env_vars == ["APP_BASE_URL", "SECRET_KEY"]  # 패치가 실제로 읽는 이름만
    assert set(out.target_hashes) == {APP} and out.target_hashes[APP].startswith("sha256:")
    assert out.attempts == 1
    data = provider.seen[0].user
    assert "    7|         SECRET_KEY=" in data and "migrations" not in data


def test_ai_sees_masked_values_and_they_never_reach_the_file(tmp_path: Path) -> None:
    original = f'import os\nSECRET_KEY = "{FAKE_SECRET}"\nHOST = "localhost"\n'
    (tmp_path / "settings.py").write_text(original, encoding="utf-8")
    edits = [
        {
            "path": "settings.py",
            "start": 2,
            "end": 2,
            "lines": ['SECRET_KEY = os.environ["SECRET_KEY"]'],
        },
        {"path": "settings.py", "start": 3, "end": 3, "lines": ['HOST = os.environ["APP_HOST"]']},
    ]
    provider = FakeProvider(reply(edits, ["SECRET_KEY", "APP_HOST"]))
    out = propose(tmp_path, provider)
    assert FAKE_SECRET not in provider.seen[0].user and "[REDACTED]" in provider.seen[0].user
    assert out.status == "proposed" and out.patch is not None
    assert "[REDACTED]" not in out.patch.decode()


def test_previous_patch_is_reused_without_ai_when_it_still_fits(source: Path) -> None:
    first = propose(source, FakeProvider(reply(GOOD_EDITS)))
    assert first.patch is not None
    previous = PreviousPatch(patch=first.patch, reason=REASON, source=Source.LIVE)
    out = propose(source, FakeProvider(), previous=previous)  # AI를 부르면 AssertionError
    assert out.status == "reused" and out.patch == first.patch and out.attempts == 0
    assert out.meta == {"reason": CHECKED_REASON, "reuse": True, "source": "live"}


def test_previous_patch_that_no_longer_fits_is_proposed_again(source: Path) -> None:
    first = propose(source, FakeProvider(reply(GOOD_EDITS)))
    assert first.patch is not None
    changed = ORIGINAL.replace("return app", "return app  # v3")
    (source / APP).write_text(changed, encoding="utf-8")
    provider = FakeProvider(reply(GOOD_EDITS))
    out = propose(source, provider, previous=PreviousPatch(first.patch, REASON))
    assert out.status == "proposed" and out.meta is not None and out.meta["reuse"] is False
    assert len(provider.seen) == 1


def test_rejected_draft_is_retried_once_with_violation_codes_only(source: Path) -> None:
    commented = [
        *GOOD_EDITS[:1],
        {
            "path": APP,
            "start": 7,
            "end": 7,
            "lines": ['        SECRET_KEY=os.environ["SECRET_KEY"],  # dev'],
        },
        *GOOD_EDITS[2:],
    ]
    provider = FakeProvider(reply(commented), reply(GOOD_EDITS))
    out = propose(source, provider)
    assert out.status == "proposed" and out.attempts == 2
    retry = provider.seen[1].user
    assert "위반 코드: comment" in retry and "# dev" not in retry.split("<untrusted_data>")[0]


def test_two_rejections_return_rejected_not_an_exception(source: Path) -> None:
    secret_default = [
        {
            "path": APP,
            "start": 7,
            "end": 7,
            "lines": ['        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),'],
        }
    ]
    out = propose(source, FakeProvider(reply(secret_default), reply(secret_default)))
    assert out.status == "rejected" and out.meta is None and out.attempts == 2
    assert out.check is not None and not out.check.passed
    assert {v.code for v in out.check.violations} >= {"secret_literal"}


def test_edit_outside_targets_or_range_is_retried(source: Path) -> None:
    outside = [{"path": "flaskr/blog.py", "start": 1, "end": 1, "lines": ["x = 2"]}]
    beyond = [{"path": APP, "start": 50, "end": 50, "lines": ["x = 2"]}]
    out = propose(source, FakeProvider(reply(outside), reply(beyond)))
    assert out.status == "rejected" and out.patch is None and out.attempts == 2


def test_masking_that_changes_line_count_returns_rejected_before_ai(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ddak.plan.patch.generate.redact", lambda text: "[REDACTED]")
    provider = FakeProvider()
    out = propose(source, provider)
    assert out.status == "rejected" and out.patch is None and out.meta is None
    assert out.attempts == 0 and provider.seen == []


def test_ai_is_only_called_inside_the_patch_config_tool(source: Path) -> None:
    with pytest.raises(DdakToolError) as caught:
        propose_config_patch(source, ON, provider=FakeProvider(reply(GOOD_EDITS)), settings=CFG)
    assert caught.value.code is ErrorCode.AI_NOT_ALLOWED


def edit(start: int, end: int, *lines: str) -> PatchEdit:
    return PatchEdit(path="a.py", start=start, end=end, lines=list(lines))


def test_apply_edits_keeps_crlf_and_missing_final_newline() -> None:
    old = "a = 1\r\nb = 'localhost'"
    changes = apply_edits(
        {"a.py": old}, [edit(2, 2, 'b = os.environ["B"]'), edit(1, 0, "import os")]
    )
    assert changes["a.py"] == (old, 'import os\r\na = 1\r\nb = os.environ["B"]')
    assert build_patch(changes).count(b"\r\n") == 2  # 원본 CRLF 줄과 새로 넣은 줄


@pytest.mark.parametrize(
    "edits",
    [
        [edit(1, 2, "x"), edit(2, 2, "y")],  # 겹침
        [edit(3, 3, "x")],  # 파일 밖
        [edit(2, 0, "x")],  # end < start - 1
    ],
)
def test_apply_edits_rejects_bad_ranges(edits: list[PatchEdit]) -> None:
    with pytest.raises(DdakToolError) as caught:
        apply_edits({"a.py": "a = 1\nb = 2\n"}, edits)
    assert caught.value.code is ErrorCode.AI_OUTPUT_INVALID


def test_draft_lines_must_be_single_lines() -> None:
    with pytest.raises(ValueError):
        PatchEdit(path="a.py", start=1, end=1, lines=["a\nb"])
