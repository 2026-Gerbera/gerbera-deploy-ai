"""source=fixture: 시연 이야기와 값 가림, 동일 승인 관문."""

import json

from ddak.core.contracts.context import RunContext
from ddak.core.snapshots import digest_bytes
from ddak.executor.presentation import masked_code
from ddak.web.story import result_story
from tests.unit.test_deployment_service import plan
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for, prepare


def test_story_approval_masks_literals_and_keeps_one_form(rig):
    service, source, _ = rig
    secret = "fixture-" + "private-value"
    address = "http://127.0.0.1:5432"
    old = f'SECRET_KEY = "{secret}"\nDB_URL = "{address}"\n'
    new = 'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\nDB_URL = os.environ["DB_URL"]\n'
    (source / "app.py").write_text(old)
    import difflib

    patch = "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/app.py", "b/app.py")
    ).encode()
    p = plan("story", patch=True)
    rid = service.prepare(
        p,
        RunContext(
            "story",
            project=p.project,
            toggles=p.toggles,
            deploy_config={"tiers": {"was": {"paths": ["."]}}},
        ),
        source,
        patch=patch,
        patch_meta={
            "reason": "환경변수 조회로 변경",
            "reuse": False,
            "source": "fixture",
            "passed": True,
            "patch_sha256": digest_bytes(patch),
            "new_env_keys": ["SECRET_KEY", "DB_URL"],
        },
    )
    directory = service.root / "runs" / rid
    (directory / "facts.json").write_text(
        json.dumps(
            {
                "patch_targets": [
                    {"file": "app.py", "line": 1, "pattern_id": "secret_key", "key": "SECRET_KEY"}
                ]
            }
        )
    )
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/approval").text
        assert secret not in html and address not in html
        assert "app.py:1" in html and "SECRET_KEY = [가림 · 하드코딩 서명 키]" in html
        assert "os.environ" in html and 'class="del"' in html and 'class="add"' in html
        assert html.count("data-approval-form") == 1
        assert html.count('value="approved"') == 2
        assert client.get(f"/ops/runs/{rid}/approval").text == html
        assert service.store.prepared(rid)["patch"] == patch.decode()
        positions = [
            html.index(text)
            for text in [
                "들어온 것",
                "찾은 것",
                "AI가 고친 것",
                "배포 계획",
                "생기거나 바뀌는 리소스",
                "승인 대상",
            ]
        ]
        assert positions == sorted(positions)


def test_masked_code_hides_comments_numbers_and_multiline():
    code = 'SECRET_KEY: str = """fixture-private\nsecond-line"""\n# private-note\nPORT=123456\n'
    safe = masked_code(code)
    assert all(
        value not in safe for value in ("fixture-private", "second-line", "private-note", "123456")
    )


def test_result_digest_checks_and_failure_order():
    run = {
        "status": "FAILED_CLOUD",
        "created": 10,
        "finished": 75,
        "context": {"source_sha": "b" * 40},
        "result": {
            "tracks": {"local": "DONE", "cloud": "ROLLED_BACK"},
            "steps": {
                "deploy.app.cloud": {"status": "failed", "elapsed_s": 3},
                "verify.health.local": {"status": "failed"},
                "verify.smoke.local": {
                    "status": "succeeded",
                    "output": {"scenarios": [{"id": "Home", "ok": True}]},
                },
            },
        },
    }
    display = {
        "previous": {
            "local": {"source_sha": "a" * 40, "images": {"was": "repo@sha256:" + "a" * 64}}
        },
        "failed_steps": ["verify.health.local", "deploy.app.cloud"],
    }
    story = result_story(run, {"images": {"was": "repo@sha256:" + "b" * 64}}, display)
    assert story["images"][0]["before"] == "a" * 12
    assert story["images"][0]["after"] == "b" * 12
    assert story["elapsed_s"] == 65
    assert story["failure"]["stage"] == "서비스 응답 확인"
    assert any(c.get("scenarios") == {"passed": 1, "total": 1} for c in story["checks"])


def test_progress_and_result_render(rig):
    service, source, _ = rig
    rid = prepare(service, source)
    service.store.finish(
        rid,
        "SUCCEEDED",
        {"tracks": {"local": "DONE"}, "steps": {}},
        {"images": {"was": "repo@sha256:" + "a" * 64}},
        {},
    )
    with client_for(service) as client:
        html = client.get(f"/runs/{rid}/result").text
        assert "a" * 12 in html and "총 소요 시간" in html and 'aria-label="환경별 결과"' in html
        assert "data-live-region" in html
        progress = client.get(f"/runs/{rid}/progress")
        assert progress.status_code == 200
        assert "<title>배포 진행 · Gerbera</title>" in progress.text
