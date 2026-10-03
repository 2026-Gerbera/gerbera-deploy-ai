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
from ddak.plan.patch.generate import _previous_files, apply_edits

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
            '        SESSION_COOKIE_SECURE=os.environ["SESSION_COOKIE_SECURE"].lower() == "true",'
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


def test_toggle_off_without_previous_patch_is_refused(source: Path) -> None:
    with pytest.raises(DdakToolError) as caught:
        propose_config_patch(source, RunContext("run-1"), provider=FakeProvider())
    assert caught.value.code is ErrorCode.TOGGLE_OFF


def test_find_targets_skips_comments_tests_and_migrations(source: Path) -> None:
    assert find_targets(source) == {APP: ["cookie_secure", "local_address", "secret_key"]}


def test_no_targets_means_no_patch_and_no_ai_call(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    out = propose(tmp_path, FakeProvider())
    assert out.status == "no_targets" and out.patch is None and out.attempts == 0


def test_lines_that_already_read_env_are_not_targets(tmp_path: Path) -> None:
    # 샘플 앱 prod(04d779d 이후)의 ProxyFix·설정 줄: 이미 환경변수로 읽으니 AI를 부르지 않는다
    (tmp_path / "config.py").write_text(
        'X = {"PROXY_FIX_X_FOR": env_int("PROXY_FIX_X_FOR", 0)}\n'
        'Y = os.environ.get("SECRET_KEY")\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        'x_for, x_proto = app.config["PROXY_FIX_X_FOR"], app.config["PROXY_FIX_X_PROTO"]\n'
        "app.wsgi_app = ProxyFix(app.wsgi_app, x_for=x_for, x_proto=x_proto)\n",
        encoding="utf-8",
    )
    assert find_targets(tmp_path) == {}
    out = propose(tmp_path, FakeProvider())
    assert out.status == "no_targets" and out.attempts == 0


def test_hardcoded_numbers_and_strings_are_targets(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)\n"
        'URL = f"http://localhost:{PORT}"\n',
        encoding="utf-8",
    )
    assert find_targets(tmp_path) == {"app.py": ["local_address", "proxy_fix"]}


def test_db_address_with_password_is_covered_by_patch_config(tmp_path: Path) -> None:
    # patch_db_access 흡수: 코드에 박힌 DB 접속 주소도 local_address 대상이다.
    # 비밀번호는 AI에 가려진다
    fake_pw = "fake-" + "db-" + "pw-123"
    original = f'DB_URL = "mysql+pymysql://app:{fake_pw}@localhost:3306/flaskr"\n'
    (tmp_path / "db.py").write_text(original, encoding="utf-8")
    assert find_targets(tmp_path) == {"db.py": ["local_address"]}
    edits = [
        {"path": "db.py", "start": 1, "end": 0, "lines": ["import os", ""]},
        {"path": "db.py", "start": 1, "end": 1, "lines": ['DB_URL = os.environ["DATABASE_URL"]']},
    ]
    provider = FakeProvider(reply(edits, ["DATABASE_URL"]))
    out = propose(tmp_path, provider)
    assert fake_pw not in provider.seen[0].user and "[REDACTED]" in provider.seen[0].user
    assert out.status == "proposed" and out.env_vars == ["DATABASE_URL"]
    assert out.patch is not None and fake_pw in out.patch.decode()  # 지운 줄에만 있다
    assert all(fake_pw not in line for line in out.patch.decode().splitlines() if line[:1] == "+")


def test_ai_may_answer_that_nothing_needs_fixing(source: Path) -> None:
    empty = json.dumps({"edits": [], "reason": "고칠 줄이 없다", "env_vars": []})
    out = propose(source, FakeProvider(empty))
    assert out.status == "no_targets" and out.patch is None and out.attempts == 1


def test_ai_edits_become_a_checked_patch_for_the_executor(source: Path) -> None:
    provider = FakeProvider(
        reply(GOOD_EDITS, ["SECRET_KEY", "APP_BASE_URL", "lower", "UNUSED_KEY"])
    )
    out = propose(source, provider)
    assert out.status == "proposed"
    assert out.check is not None and out.check.passed, out.check
    assert out.patch is not None and out.patch.startswith(b"--- a/flaskr/__init__.py\n")
    assert b"+import os\n" in out.patch and b'-        SECRET_KEY="dev",\n' in out.patch
    assert out.meta == {"reason": REASON, "reuse": False, "source": "replay"}
    encode_meta(out.meta)  # 실행기 prepare(patch_meta=)가 받는 모양이다
    # 추가한 줄이 실제로 읽는 이름(AI가 빠뜨린 SESSION_COOKIE_SECURE 포함, lower·UNUSED_KEY 제외)
    assert out.env_vars == ["APP_BASE_URL", "SECRET_KEY", "SESSION_COOKIE_SECURE"]
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
    assert out.meta == {"reason": REASON, "reuse": True, "source": "live"}


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


def test_masking_that_changes_line_count_stops_before_ai(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ddak.plan.patch.generate.redact", lambda text: "[REDACTED]")
    with pytest.raises(DdakToolError) as caught:
        propose(source, FakeProvider())
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED


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


# ---- 10/3 결정 12: 파일 단위 재적용, 패치 손실 관문, 토글 OFF -------------------------

SETTINGS = "flaskr/settings.py"
SETTINGS_ORIGINAL = 'HOST = "localhost"\n'
SETTINGS_EDITS = [
    {"path": SETTINGS, "start": 1, "end": 0, "lines": ["import os", ""]},
    {"path": SETTINGS, "start": 1, "end": 1, "lines": ['HOST = os.environ["APP_HOST"]']},
]
OFF = RunContext("run-1")


def first_patch(source: Path) -> PreviousPatch:
    out = propose(source, FakeProvider(reply(GOOD_EDITS)))
    assert out.status == "proposed" and out.patch is not None
    return PreviousPatch(patch=out.patch, reason=REASON, source=Source.LIVE)


def propose_off(source: Path, previous: PreviousPatch):  # type: ignore[no-untyped-def]
    # 토글 OFF는 AI를 부르지 않으므로 tool_context 없이도 돈다(AI 호출 시 AI_NOT_ALLOWED로 드러남)
    return propose_config_patch(source, OFF, previous=previous, provider=FakeProvider())


def test_previous_files_round_trip_keeps_crlf_and_missing_newline() -> None:
    old = "a = 1\r\nb = 'localhost'"
    new = 'import os\r\na = 1\r\nb = os.environ["B"]'
    files = _previous_files(build_patch({"a.py": (old, new)}))
    assert set(files) == {"a.py"}
    assert (files["a.py"].old, files["a.py"].new) == (old, new)
    assert files["a.py"].removed == ("b = 'localhost'",)


def test_reapply_is_per_file_and_ai_sees_only_the_changed_file(source: Path) -> None:
    (source / SETTINGS).write_text(SETTINGS_ORIGINAL, encoding="utf-8")
    both = propose(source, FakeProvider(reply([*GOOD_EDITS, *SETTINGS_EDITS])))
    assert both.status == "proposed" and both.patch is not None
    previous = PreviousPatch(patch=both.patch, reason=REASON)
    # 새 prod: settings.py만 바뀌었다. __init__.py는 그대로라 AI 없이 재적용한다
    (source / SETTINGS).write_text('# v3\nHOST = "localhost"\n', encoding="utf-8")
    edits = [
        {"path": SETTINGS, "start": 1, "end": 0, "lines": ["import os"]},
        {"path": SETTINGS, "start": 2, "end": 2, "lines": ['HOST = os.environ["APP_HOST"]']},
    ]
    provider = FakeProvider(reply(edits, ["APP_HOST"]))
    out = propose(source, provider, previous=previous)
    assert out.status == "proposed" and out.reapplied == [APP] and out.lost == []
    sent = provider.seen[0].user
    assert SETTINGS in sent and APP not in sent  # 바뀐 파일만 AI에 간다
    assert out.patch is not None and out.check is not None and out.check.passed
    assert b"--- a/flaskr/__init__.py" in out.patch and b"--- a/flaskr/settings.py" in out.patch


def test_toggle_off_reapplies_previous_patch_without_ai(source: Path) -> None:
    previous = first_patch(source)
    out = propose_off(source, previous)
    assert out.status == "reused" and out.patch == previous.patch and out.attempts == 0
    assert out.meta == {"reason": REASON, "reuse": True, "source": "live"}
    assert out.env_vars == ["APP_BASE_URL", "SECRET_KEY", "SESSION_COOKIE_SECURE"]


def test_toggle_off_with_changed_file_is_patch_lost_not_silent_removal(source: Path) -> None:
    previous = first_patch(source)
    (source / APP).write_text(ORIGINAL.replace("return app", "return app  # v3"), encoding="utf-8")
    out = propose_off(source, previous)  # AI를 부르면 FakeProvider가 AssertionError
    assert out.status == "patch_lost" and out.lost == [APP] and out.attempts == 0


def test_ai_that_leaves_a_removed_value_is_patch_lost(source: Path) -> None:
    previous = first_patch(source)
    (source / APP).write_text(ORIGINAL.replace("return app", "return app  # v3"), encoding="utf-8")
    keeps_dev = [e for e in GOOD_EDITS if e["start"] != 7]  # SECRET_KEY="dev" 줄을 그대로 둔다
    out = propose(source, FakeProvider(reply(keeps_dev)), previous=previous)
    assert out.status == "patch_lost" and out.lost == [APP] and out.attempts == 1


def test_ai_saying_nothing_to_fix_cannot_drop_the_previous_patch(source: Path) -> None:
    previous = first_patch(source)
    (source / APP).write_text(ORIGINAL.replace("return app", "return app  # v3"), encoding="utf-8")
    empty = json.dumps({"edits": [], "reason": "고칠 줄이 없다", "env_vars": []})
    out = propose(source, FakeProvider(empty), previous=previous)
    assert out.status == "patch_lost" and out.lost == [APP]


def test_value_fixed_by_developer_is_not_a_loss(source: Path) -> None:
    previous = first_patch(source)
    # 개발자가 prod에서 직접 환경변수로 바꿨다: 값 줄이 없으니 대상도, 손실도 없다
    (source / APP).write_text(
        "import os\n\nfrom flask import Flask\n\n\ndef create_app():\n"
        "    app = Flask(__name__)\n"
        '    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]\n'
        "    return app\n",
        encoding="utf-8",
    )
    out = propose(source, FakeProvider(), previous=previous)
    assert out.status == "no_targets" and out.lost == [] and out.attempts == 0


def _old_rule_patch() -> PreviousPatch:
    # 결정 12 전 규칙으로 승인된 패치: 비밀이 아닌 키에 개발용 기본값(localhost)을 남겼다
    new = ORIGINAL.replace(
        '    app.config["APP_BASE_URL"] = "http://localhost:5000"',
        '    app.config["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "http://localhost:5000")',
    ).replace("from flask import Flask\n", "import os\n\nfrom flask import Flask\n")
    return PreviousPatch(patch=build_patch({APP: (ORIGINAL, new)}), reason=REASON)


def test_old_rule_patch_is_proposed_again_when_toggle_on(source: Path) -> None:
    provider = FakeProvider(reply(GOOD_EDITS))
    out = propose(source, provider, previous=_old_rule_patch())
    assert out.status == "proposed" and out.reapplied == [] and len(provider.seen) == 1
    assert out.patch is not None and b"os.environ.get" not in out.patch


def test_old_rule_patch_is_patch_lost_when_toggle_off(source: Path) -> None:
    out = propose_off(source, _old_rule_patch())
    assert out.status == "patch_lost" and out.lost == [APP]
