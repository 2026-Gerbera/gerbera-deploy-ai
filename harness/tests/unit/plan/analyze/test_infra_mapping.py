"""표시용 리소스 제안의 호출 조건·실패 격리·승인 전달."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.runtime import tool_context
from ddak.plan import flow
from ddak.plan.analyze import suggest_infra_mappings
from tests.unit.plan.test_flow import V1, commit, go, good_draft
from tests.unit.test_deployment_service import plan
from tests.unit.test_deployment_service import rig as rig


class FakeProvider:
    name = "fixture"

    def __init__(self, *, error: Exception | None = None, target: str = "elasticache"):
        self.calls: list[AIRequest] = []
        self.error = error
        self.target = target

    def complete(self, req: AIRequest) -> AIResponse:
        self.calls.append(req)
        if self.error:
            raise self.error
        return AIResponse(
            text=json.dumps(
                {
                    "mappings": [
                        {
                            "evidence_id": "resource-1",
                            "target": self.target,
                            "reason": "관리형 리소스 후보. 자동 생성 미지원",
                        },
                    ]
                }
            ),
            source=Source.FIXTURE,
        )


def suggest(root: Path, provider: FakeProvider):
    with tool_context("analyze_project", "run-1"):
        return suggest_infra_mappings(root, settings=Settings(), provider=provider)


def test_handled_rules_do_not_call_provider(tmp_path):
    (tmp_path / "app.py").write_text(
        'import os\nIMG_DIR = "img"\nDATABASE_URL = "sqlite:///local.db"\n'
        'SECRET_KEY = "dev"\nAPP_BASE_URL = "http://localhost:5000"\n'
        "SESSION_COOKIE_SECURE = False\nProxyFix(app, x_for=1)\n"
        'db = os.environ.get("DATABASE_URL", "sqlite:///another.db")\n'
    )
    provider = FakeProvider()
    assert suggest(tmp_path, provider) == []
    assert provider.calls == []


@pytest.mark.parametrize(
    ("literal", "kind"),
    [
        ("redis://localhost:6379", "redis"),
        ("127.0.0.1:6379", "redis"),
        ("amqp://localhost", "broker"),
        ("localhost:25", "mail"),
        ("data/cache.sqlite", "sqlite"),
    ],
)
def test_resource_input_is_metadata_only(tmp_path, literal, kind):
    (tmp_path / "app.py").write_text(f"client.connect({literal!r})\n")
    provider = FakeProvider()
    result = suggest(tmp_path, provider)
    assert result == [
        {
            "kind": kind,
            "file": "app.py",
            "line": 1,
            "target": "elasticache",
            "reason": "관리형 리소스 후보. 자동 생성 미지원",
            "executable": False,
        }
    ]
    request = provider.calls[0]
    data = json.loads(request.user.split("<untrusted_data>\n")[1].split("\n</untrusted_data>")[0])
    assert data == [
        {"evidence_id": "resource-1", "kind": kind, "file": "app.py", "line": 1, "key": None}
    ]
    assert literal not in request.user and "client.connect" not in request.user
    assert request.timeout_s == 5


def test_disallowed_target_dropped(tmp_path):
    (tmp_path / "app.py").write_text('client.connect("127.0.0.1:6379")\n')
    assert suggest(tmp_path, FakeProvider(target="lambda")) == []


@pytest.mark.parametrize("error", [TimeoutError("private detail"), ValueError("private detail")])
def test_failure_does_not_change_analysis_or_plan(tmp_path, monkeypatch, capsys, error):
    repo = tmp_path / "repo"
    commit(repo, {**V1, "was/resource.py": 'client.connect("127.0.0.1:6379")\n'})
    monkeypatch.setattr(
        flow,
        "generate_plan",
        lambda *a, **k: type(
            "Result",
            (),
            {
                "draft": good_draft(),
            },
        )(),
    )
    analyzed = []
    original = flow.analyze_project

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        analyzed.append(result)
        return result

    monkeypatch.setattr(flow, "analyze_project", capture)
    succeeded = go(tmp_path / "ok", repo, provider=FakeProvider())
    provider = FakeProvider(error=error)
    failed = go(tmp_path / "failed", repo, provider=provider)
    assert analyzed[0] == analyzed[1]
    assert succeeded.facts == failed.facts
    assert succeeded.plan == failed.plan
    assert (
        succeeded.context.project_settings["_infra_mapping_suggestions"][0]["executable"] is False
    )
    assert "_infra_mapping_suggestions" not in failed.context.project_settings
    assert len(provider.calls) == 1
    logs = capsys.readouterr().err
    assert "리소스 매핑 제안 생략" in logs and "private detail" not in logs


def test_approval_data_is_persisted_without_infra_subject(rig):
    service, source, _ = rig
    p = plan()
    suggestions = [
        {
            "kind": "redis",
            "file": "app.py",
            "line": 1,
            "target": "elasticache",
            "reason": "관리형 캐시 후보",
            "executable": False,
        }
    ]
    service.store.save_project_settings(
        p.project, {"auto_detect": False}, updated_by="operator", expected_version=0
    )
    context = RunContext(
        p.run_id, project=p.project, project_settings={"_infra_mapping_suggestions": suggestions}
    )
    run_id = service.prepare(p, context, source)
    view = service.approval_view(run_id)
    assert view["infra"]["mapping_suggestions"] == suggestions
    assert view["infra_summary"] is None and "infra" not in view["subjects"]
    saved = json.loads((service.root / "runs" / run_id / "approval-view.json").read_text())
    assert saved["infra"] == view["infra"]
    assert service.approve(run_id, approver="operator")
