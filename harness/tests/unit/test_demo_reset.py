"""source=fixture: 메모리 Git·GitHub·HTTP로 시연 초기화 경로를 확인한다."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ddak.core import demo_backend as backend_module
from ddak.core.config import Settings
from ddak.core.contracts.errors import DdakToolError
from ddak.core.demo_backend import DemoBackend, api_token, probe_version
from ddak.core.demo_reset import DemoReset
from ddak.web.app import create_app
from ddak.web.routes.ops import router

BASE = "http://127.0.0.1:8765"
SHA1, SHA2, MERGE = "1" * 40, "2" * 40, "3" * 40
T1, T2 = "a" * 40, "b" * 40
PROJECT = "flaskr-three"
SAVED = dict(
    repo_url="https://github.com/example/app.git",
    watch_branch="prod",
    default_targets="both",
    auto_detect=True,
    git_author_name="Demo Operator",
    git_author_email="operator@example.test",
)


class FakeGit:
    credential_source = "managed"

    def __init__(self):
        self.calls = []
        self.prod = SHA2
        self.trees = {SHA1: T1, SHA2: T2}
        self.tags = {"v1": SHA1, "v2": SHA2}
        self.parents = {}
        self.refs = {}

    def git(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-parse":
            ref = args[-1]
            ref = ref.replace("refs/remotes/origin/prod", self.prod)
            for tag, sha in self.tags.items():
                ref = ref.replace("refs/tags/" + tag, sha)
            if ref.startswith("refs/tags/"):
                return ""
            if ref.endswith("^1^{tree}"):
                return self.trees[self.parents[ref.removesuffix("^1^{tree}")]]
            if ref.endswith("^{tree}"):
                return self.trees.get(ref[:-7], "")
            if ref.endswith("^{commit}"):
                return ref[:-9]
            if ref.endswith("^1"):
                return self.parents[ref[:-2]]
        if args[0] == "commit-tree":
            sha = str(len(self.parents) + 4) * 40
            self.trees[sha] = args[1]
            self.parents[sha] = args[args.index("-p") + 1]
            return sha
        if args[0] == "ls-remote":
            if args[-1].startswith("refs/tags/"):
                return self.tags.get(args[-1].removeprefix("refs/tags/"), "")
            return self.refs.get(args[-1], "")
        if args[0] == "push":
            sha, ref = args[-1].split(":")
            self.refs[ref] = sha
            return ""
        raise AssertionError(args)


class FakeGithub:
    def __init__(self, git):
        self.git = git
        self.prs = []
        self.creates = []

    def open_prs(self):
        return [p for p in self.prs if p["state"] == "open"]

    def create(self, branch, title, body):
        self.creates.append((branch, title, body))
        pr = dict(
            number=len(self.prs) + 1,
            state="open",
            merged=False,
            head=dict(ref=branch, sha=self.git.refs["refs/heads/" + branch]),
        )
        self.prs.append(pr)
        return pr

    def get(self, number):
        return self.prs[number - 1]


@pytest.fixture
def backend(tmp_path):
    git = FakeGit()
    github = FakeGithub(git)
    connects = []

    def connect(*args, **kwargs):
        connects.append((args, kwargs))
        return git

    return DemoBackend(tmp_path, connect=connect, github=lambda *a: github), git, github, connects


def test_reset_then_v2_reuses_cli_tree_logic_without_prod_tag_writes(backend):
    service, git, github, connects = backend
    reset = service.create(PROJECT, SAVED, "reset-v1")
    head = git.refs["refs/heads/" + reset["branch"]]
    assert git.trees[head] == T1
    assert git.parents[head] == SHA2
    assert reset["url"] == "https://github.com/example/app/pull/1"
    # 같은 기준의 PR은 재사용한다.
    assert service.create(PROJECT, SAVED, "reset-v1")["number"] == reset["number"]
    assert len(github.creates) == 1
    github.prs[0].update(state="closed", merged=True, merge_commit_sha=head)
    git.prod = head
    v2 = service.create(PROJECT, SAVED, "prepare-v2")
    v2head = git.refs["refs/heads/" + v2["branch"]]
    assert git.trees[v2head] == T2 and git.parents[v2head] == head
    for args in git.calls:
        assert args[0] not in ("config", "tag", "merge")
        if args[0] == "push":
            assert "--no-follow-tags" in args
            assert ":refs/heads/demo/" in args[-1]
            assert not any(a.startswith(("+", "--force")) for a in args)
    assert connects[0][1]["author"] == ("Demo Operator", "operator@example.test")
    assert connects[0][1]["credentials"][1:] == (PROJECT, SAVED["repo_url"])
    assert git.prod == head


def test_prod_already_v1_reports_source_only(backend):
    service, git, github, _ = backend
    git.prod = SHA1
    out = service.create(PROJECT, SAVED, "reset-v1")
    assert out["already_source"] and out["merge_sha"] == SHA1
    assert github.creates == []
    assert all(args[0] != "push" for args in git.calls)


def test_inspect_merged_commit_and_tag_tree_labels(backend):
    service, git, github, _ = backend
    reset = service.create(PROJECT, SAVED, "reset-v1")
    head = git.refs["refs/heads/" + reset["branch"]]
    github.prs[0].update(state="closed", merged=True, merge_commit_sha=head)
    info = service.inspect(PROJECT, SAVED, {"reset-v1": reset}, {SHA1, SHA2, head})
    assert info["prs"]["reset-v1"]["merge_sha"] == head
    assert info["labels"] == {SHA1: "v1", SHA2: "v2", head: "v1"}


class FakeDeployment:
    def __init__(self, root):
        self.root = root
        self.saved = SAVED.copy()
        self.runs, self.environments, self.requests = {}, {}, []

    def resolve_project(self, project):
        return project

    def get_project_settings(self, project):
        return self.saved

    def get_environments(self, project):
        return self.environments

    def get_run(self, rid):
        return self.runs[rid]

    def list_runs(self, limit=20, *, project=None):
        # 실제 DeploymentService처럼 프로젝트 필터와 개수 제한을 받는다.
        rows = list(reversed(list(self.runs.values())))
        return [row for row in rows if project is None or row["project"] == project][:limit]

    def list_pending_runs(self, project):
        return []

    def list_preparations(self, project):
        return self.requests

    def project_state(self, project):
        return dict(blocked_targets=[], active_runs=[])

    async def shutdown(self):
        pass

    def enqueue_deployment(self, project, **kwargs):
        self.requests.append(
            {
                "project": project,
                "targets": self.saved["default_targets"],
                "request_id": "prep-fixture",
                "status": "PREPARING",
                **kwargs,
            }
        )


class FakeBackend:
    already = False
    merged = False

    def create(self, project, saved, action):
        out = dict(
            tag="v1" if action == "reset-v1" else "v2", target_tree=T1, repo_url=saved["repo_url"]
        )
        if self.already:
            return {**out, "already_source": True, "merge_sha": MERGE}
        return {**out, "number": 1, "url": "https://github.com/example/app/pull/1"}

    def inspect(self, project, saved, records, sources):
        return {
            "prs": {
                name: {
                    "merged": self.merged or self.already,
                    "merge_sha": MERGE if self.merged or self.already else None,
                }
                for name in records
            },
            "labels": {MERGE: "v1", SHA2: "v2"},
        }


@pytest.fixture
def rig(tmp_path):
    deployment = FakeDeployment(tmp_path)
    backend = FakeBackend()
    live = {"local": {"release_id": "old"}, "cloud": {"release_id": "old"}}
    demo = DemoReset(
        deployment,
        backend=backend,
        probe=lambda url: live[url.split("//")[1].split(".")[0]],
        urls=lambda *_: {"local": "https://local.example", "cloud": "https://cloud.example"},
    )
    deployment.demo_reset = demo
    return deployment, demo, backend, live


def mark_run(deployment, status):
    deployment.runs["run-demo"] = dict(
        run_id="run-demo",
        project=PROJECT,
        status=status,
        context={"source_sha": MERGE, "ref": "prod"},
    )


def complete(deployment, live, env):
    deployment.environments[env] = {
        "status": "SUCCEEDED",
        "current": {"release_id": "run-demo", "source_sha": MERGE, "candidate_sha": "c" * 40},
    }
    live[env] = {"release_id": "run-demo", "source_sha": "c" * 40}


def test_reset_progress_requires_selected_environments_and_live_version(rig):
    deployment, demo, backend, live = rig
    demo.create(PROJECT, "reset-v1")
    assert demo.view(PROJECT)["actions"][0]["stage"] == "PR 열림"
    backend.merged = True
    assert demo.view(PROJECT)["actions"][0]["stage"] == "merge됨"
    mark_run(deployment, "AWAITING_APPROVAL")
    assert demo.view(PROJECT)["actions"][0]["stage"] == "승인 대기"
    mark_run(deployment, "RUNNING")
    assert demo.view(PROJECT)["actions"][0]["stage"] == "배포 진행 중"
    mark_run(deployment, "SUCCEEDED")
    complete(deployment, live, "local")
    assert demo.view(PROJECT)["actions"][0]["stage"] != "v1 배포 완료"
    complete(deployment, live, "cloud")
    live["cloud"]["release_id"] = "old"
    assert demo.view(PROJECT)["actions"][0]["stage"] != "v1 배포 완료"
    live["cloud"]["release_id"] = "run-demo"
    view = demo.view(PROJECT)
    assert view["actions"][0]["stage"] == "v1 배포 완료"
    assert all(e["version"] == "v1" for e in view["environments"])
    assert view["environments"][0]["sha"] == MERGE
    # 재시작 뒤에도 공개 PR 기록을 읽는다.
    assert DemoReset(deployment, backend=backend)._records(PROJECT)["reset-v1"]["number"] == 1


def test_onprem_does_not_wait_for_cloud(rig):
    deployment, demo, backend, live = rig
    deployment.saved["default_targets"] = "onprem"
    demo.create(PROJECT, "reset-v1")
    backend.merged = True
    mark_run(deployment, "SUCCEEDED")
    complete(deployment, live, "local")
    view = demo.view(PROJECT)
    assert view["actions"][0]["stage"] == "v1 배포 완료"
    assert [e["environment"] for e in view["environments"]] == ["local"]


def test_missing_watch_or_failed_probe_never_claims_complete(rig):
    deployment, demo, backend, _ = rig
    deployment.saved["auto_detect"] = False
    with pytest.raises(DdakToolError, match="prod 자동 감시"):
        demo.create(PROJECT, "reset-v1")
    deployment.saved["auto_detect"] = True
    demo.create(PROJECT, "reset-v1")
    backend.merged = True
    mark_run(deployment, "FAILED_LOCAL")
    assert "FAILED_LOCAL" in demo.view(PROJECT)["actions"][0]["stage"]
    demo.probe = lambda _: (_ for _ in ()).throw(OSError("private stderr"))
    view = demo.view(PROJECT)
    assert "private stderr" not in str(view)
    assert all(not e["confirmed"] for e in view["environments"])


def client_for(deployment):
    app = create_app(deployment_factory=lambda: deployment, settings=Settings())
    app.include_router(router)
    return TestClient(app, base_url=BASE)


def post(client, action):
    return client.post(
        "/ops/demo/" + action,
        data={
            "project": PROJECT,
            "csrf_token": client.cookies["ddak_csrf"],
            "_form_id": "demo-" + action,
            "_return_to": "/ops?project=" + PROJECT,
        },
        headers={"origin": BASE, "accept": "text/html"},
        follow_redirects=False,
    )


def test_ops_buttons_pr_link_approval_and_version_html(rig):
    deployment, _, backend, live = rig
    with client_for(deployment) as client:
        page = client.get("/ops?project=" + PROJECT)
        assert page.status_code == 200
        for text in ("시연 초기화", "v1으로 되돌리기", "v2 시연 PR 준비", "온프레미스·클라우드"):
            assert text in page.text
        assert post(client, "reset-v1").status_code == 303
        status = client.get("/ops/demo/status?project=" + PROJECT)
        assert status.status_code == 200 and "PR #1 열기" in status.text
        backend.merged = True
        mark_run(deployment, "AWAITING_APPROVAL")
        status = client.get("/ops/demo/status?project=" + PROJECT)
        assert "/runs/run-demo/approval" in status.text
        complete(deployment, live, "local")
        complete(deployment, live, "cloud")
        mark_run(deployment, "SUCCEEDED")
        status = client.get("/ops/demo/status?project=" + PROJECT)
        assert "v1 배포 완료" in status.text and MERGE[:7] in status.text
        assert "https://local.example/version" in status.text
        assert "https://cloud.example/version" in status.text


def test_source_already_reset_still_requests_deployment_using_project_targets(rig):
    deployment, _, backend, _ = rig
    backend.already = True
    deployment.saved["default_targets"] = "onprem"
    with client_for(deployment) as client:
        client.get("/ops?project=" + PROJECT)
        assert post(client, "reset-v1").status_code == 303
    assert deployment.requests[0]["targets"] == "onprem"
    assert deployment.requests[0]["ref"] == "prod"


def test_csrf_and_inline_errors_are_preserved(rig):
    deployment, _, _, _ = rig
    with client_for(deployment) as client:
        client.get("/ops?project=" + PROJECT)
        rejected = client.post(
            "/ops/demo/reset-v1",
            data={"project": PROJECT},
            headers={"origin": "https://outside.invalid"},
        )
        assert rejected.status_code == 403
        deployment.saved["auto_detect"] = False
        response = post(client, "reset-v1")
        assert response.status_code == 303
        html = client.get(response.headers["location"]).text
        assert "prod 자동 감시" in html


def test_managed_token_precedes_machine_and_never_appears_in_git_argv(monkeypatch, tmp_path):
    calls = []
    token = "fixture-" + "credential"
    monkeypatch.setattr(backend_module, "require_token", lambda *a: token)
    monkeypatch.setattr(backend_module.subprocess, "run", lambda *a, **k: calls.append(a))
    assert (
        api_token(
            SimpleNamespace(credential_source="managed"), (tmp_path, PROJECT, SAVED["repo_url"])
        )
        == token
    )
    assert calls == []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="username=operator\npassword=" + token + "\n")

    monkeypatch.setattr(backend_module.subprocess, "run", run)
    assert (
        api_token(
            SimpleNamespace(credential_source="machine", path=tmp_path),
            (tmp_path, PROJECT, SAVED["repo_url"]),
        )
        == token
    )
    assert token not in str(calls)
    assert calls[0][0][-2:] == ["credential", "fill"]


def test_version_response_exposes_only_public_identity(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "read_json",
        lambda *_: {
            "release_id": "run-test",
            "source_sha": SHA1,
            "password": "not-public",
            "version": "v1",
            "database": {"url": "private"},
        },
    )
    assert probe_version("https://fixture.example") == {
        "release_id": "run-test",
        "source_sha": SHA1,
        "version": "v1",
    }


def test_fake_mode_does_not_connect(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("network")

    backend = DemoBackend(tmp_path, connect=forbidden, enabled=False)
    with pytest.raises(DdakToolError, match="FAKE"):
        backend.create(PROJECT, SAVED, "reset-v1")


def test_cli_still_uses_same_tree_and_push_helpers(monkeypatch, tmp_path):
    from tests.support import load_script

    cli = load_script("demo_cycle")
    repo = FakeGit()
    monkeypatch.setattr(cli, "git", lambda clone, *a, **kw: repo.git(*a, **kw))
    snap = cli.snapshot(tmp_path)
    commit = cli.build_commit(tmp_path, snap, "reset-v1", cli.ACTIONS["reset-v1"])
    cli.push_branch(tmp_path, commit, "demo/reset-v1-fixture")
    assert repo.trees[commit] == T1 and repo.parents[commit] == SHA2
    assert repo.refs == {"refs/heads/demo/reset-v1-fixture": commit}


def test_v3_missing_does_not_break_v1_v2_and_v3_create_explains(backend):
    service, git, github, _ = backend
    info = service.inspect(PROJECT, SAVED, {}, {SHA1, SHA2})
    assert info["available_tags"] == ["v1", "v2"]
    with pytest.raises(DdakToolError, match="v3 태그 없음"):
        service.create(PROJECT, SAVED, "prepare-v3")
    assert not github.creates
    assert not any("refs/tags/v3:refs/tags/v3" in call for call in git.calls)
    assert service.create(PROJECT, SAVED, "prepare-v2")["already_source"]
    assert service.create(PROJECT, SAVED, "reset-v1")["tag"] == "v1"


def test_v3_pr_fetch_label_and_reset_preserve_refs(backend):
    service, git, github, _ = backend
    sha3, tree3 = "6" * 40, "d" * 40
    git.tags["v3"], git.trees[sha3] = sha3, tree3
    out = service.create(PROJECT, SAVED, "prepare-v3")
    head = git.refs["refs/heads/" + out["branch"]]
    assert out["tag"] == "v3" and out["branch"].startswith("demo/v3-")
    assert git.trees[head] == tree3 and git.parents[head] == SHA2
    assert any("refs/tags/v3:refs/tags/v3" in call for call in git.calls)
    assert github.creates[0][1] == "v3: 이미지 업로드·조회 추가"
    github.prs[0].update(state="closed", merged=True, merge_commit_sha=head)
    git.prod = head
    info = service.inspect(PROJECT, SAVED, {"prepare-v3": out}, {head})
    assert info["labels"][head] == "v3"
    reset = service.create(PROJECT, SAVED, "reset-v1")
    reset_head = git.refs["refs/heads/" + reset["branch"]]
    assert git.trees[reset_head] == T1 and git.parents[reset_head] == head
    assert git.tags == {"v1": SHA1, "v2": SHA2, "v3": sha3}
    assert git.prod == head
    assert all(
        ":refs/heads/demo/" in call[-1]
        and "--no-follow-tags" in call
        and not any(a.startswith(("+", "--force")) for a in call)
        for call in git.calls
        if call[0] == "push"
    )


@pytest.mark.parametrize("available", [False, True])
def test_v3_ops_action_and_tag_availability(rig, available):
    deployment, _demo, backend, _ = rig
    original = backend.inspect
    backend.inspect = lambda *args: {
        **original(*args),
        "available_tags": ["v1", "v2", "v3"] if available else ["v1", "v2"],
    }

    def create(project, saved, action):
        assert action == "prepare-v3"
        if not available:
            raise DdakToolError(backend_module.ErrorCode.PRECONDITION_FAILED, "v3 태그 없음")
        return dict(
            tag="v3",
            target_tree="d" * 40,
            repo_url=saved["repo_url"],
            number=1,
            url="https://github.com/example/app/pull/1",
        )

    backend.create = create
    with client_for(deployment) as client:
        page = client.get("/ops?project=" + PROJECT)
        assert 'action="/ops/demo/prepare-v3"' in page.text
        assert 'id="demo-prepare-v3"' in page.text and "disabled>v3 태그 확인 중" in page.text
        status = client.get("/ops/demo/status?project=" + PROJECT)
        assert ('data-demo-v3="ready"' if available else 'data-demo-v3="missing"') in status.text
        result = post(client, "prepare-v3")
        assert result.status_code == 303
        if available:
            status = client.get("/ops/demo/status?project=" + PROJECT)
            assert "v3 시연: PR 열림" in status.text
        else:
            assert "v3 태그 없음" in client.get(result.headers["location"]).text


def test_cli_accepts_v3_and_reports_missing_tag(monkeypatch, tmp_path, capsys):
    from contextlib import contextmanager

    from tests.support import load_script

    cli = load_script("demo_cycle")
    repo = FakeGit()

    @contextmanager
    def cloned(_):
        yield tmp_path

    monkeypatch.setattr(cli, "cloned", cloned)
    monkeypatch.setattr(cli, "git", lambda clone, *a, **kw: repo.git(*a, **kw))
    assert cli.main(["prepare-v3", "--dry-run", "--repo", "example/app"]) == 1
    assert "v3 태그 없음" in capsys.readouterr().err
    repo.tags["v3"], repo.trees["6" * 40] = "6" * 40, "d" * 40
    monkeypatch.setattr(cli, "open_demo_prs", lambda *a: [])
    assert cli.main(["prepare-v3", "--dry-run", "--repo", "example/app"]) == 0
    assert "push 예정: demo/v3-" in capsys.readouterr().out
    assert all(call[0] != "push" for call in repo.calls)


@pytest.mark.parametrize(
    "state,label,disabled",
    [
        ("ready", "v3 시연 PR 준비", False),
        ("missing", "v3 태그 없음", True),
        ("unknown", "v3 태그 확인 실패", True),
    ],
)
def test_v3_button_status_script_without_window(state, label, disabled):
    import json
    import shutil
    import subprocess

    from tests.support import REPO_ROOT

    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    script = r"""
const fs=require('fs'), vm=require('vm');
const button={disabled:true,textContent:''};
const box={dataset:{url:'/fixture'},querySelector:()=>({dataset:{demoV3:process.argv[2]}})};
const document={getElementById:id=>id==='demo-state'?box:button};
const context={document,fetch:async()=>({ok:true,text:async()=>'<fixture>'})};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
setImmediate(()=>process.stdout.write(JSON.stringify(button)));
"""
    output = subprocess.run(
        [node, "-e", script, str(REPO_ROOT.parent / "src/ddak/web/static/demo_reset.js"), state],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert json.loads(output) == {"disabled": disabled, "textContent": label}
