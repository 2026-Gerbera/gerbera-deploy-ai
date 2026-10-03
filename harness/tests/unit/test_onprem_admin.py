"""관리 페이지에서 설정한 값의 실행 경계. 외부 서비스는 가짜만 사용한다."""

from dataclasses import replace

import pytest

from ddak import app
from ddak.core.app_repository import AppRepository
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.git_credentials import configured_identity, require_token, respond, save_token
from ddak.core.private_values import SecretVault
from ddak.core.project_settings import ProjectSettings
from tests.unit.test_setup_service_fix11 import rig  # noqa: F401
from tests.unit.test_setup_web_fix11 import panel, post  # noqa: F401

URL = "https://github.com/fixture/app.git"
PRIVATE = "fixture-" + "not-a-real-git-credential"


def test_missing_identity_and_bad_characters():
    with pytest.raises(DdakToolError, match="작성자"):
        configured_identity({})
    for values in ({"git_author_name": "name\ncontrol"}, {"git_author_email": "not-an-email"}):
        with pytest.raises(ValueError):
            ProjectSettings(**values)
    assert configured_identity(
        {"git_author_name": "Operator", "git_author_email": "op@example.test"}
    ) == ("Operator", "op@example.test")


def test_token_bound_to_project_repository_and_private_storage(tmp_path):
    root = tmp_path / "private"
    save_token(root, "flaskr", URL, PRIVATE)
    assert require_token(root, "flaskr", URL) == PRIVATE
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())
    for project, url in (("other", URL), ("flaskr", URL.replace("app.git", "other.git"))):
        with pytest.raises(DdakToolError):
            require_token(root, project, url)
    good = "protocol=https\nhost=github.com\npath=fixture/app.git\n\n"
    assert respond(root, "flaskr", URL, "get", good) == (
        "username=x-access-token\npassword=" + PRIVATE + "\n\n"
    )
    for request in (
        good.replace("github.com", "evil.test"),
        good.replace("app.git", "other.git"),
        good.replace("https", "http"),
        good + "x" * 8192,
    ):
        assert respond(root, "flaskr", URL, "get", request) == ""
    assert respond(root, "flaskr", URL, "store", good) == ""


def test_real_factory_blocks_before_clone_and_uses_saved_identity(tmp_path, monkeypatch):
    factory = app._repository_factory(tmp_path / "repos", vault_root=tmp_path / "private")
    ctx = RunContext("test-run", AdapterMode.REAL, project="flaskr", repo_url=URL)
    calls = []
    monkeypatch.setattr(AppRepository, "connect", lambda *args, **kw: calls.append((args, kw)))
    with pytest.raises(DdakToolError, match="작성자"):
        factory(ctx)
    cfg = {"git_author_name": "Operator", "git_author_email": "op@example.test"}
    ctx = replace(ctx, project_settings=cfg)
    with pytest.raises(DdakToolError, match="push 토큰"):
        factory(ctx)
    assert not calls
    save_token(tmp_path / "private", "flaskr", URL, PRIVATE)
    factory(ctx)
    assert calls[0][1]["author"] == ("Operator", "op@example.test")
    assert calls[0][1]["credentials"] == (tmp_path / "private", "flaskr", URL)
    assert PRIVATE not in repr(calls)


def test_git_options_only_commit_commands_and_never_token_in_env_argv(tmp_path, monkeypatch):
    from ddak.core import app_repository

    calls = []

    class Process:
        returncode = 0

        def communicate(self, **kwargs):
            return b"fixture", b""

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(app_repository.subprocess, "Popen", popen)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "inherited")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "inherited@example.test")
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    repo = AppRepository(
        tmp_path,
        author=("Operator", "op@example.test"),
        credentials=(tmp_path / "private", "flaskr", URL),
    )
    repo.git("commit-tree", "a" * 40)
    argv, kw = calls[-1]
    assert "user.name=Operator" in argv and "user.email=op@example.test" in argv
    assert not any(k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_TRACE")) for k in kw["env"])
    assert "credential.helper=" in argv and "credential.useHttpPath=true" in argv
    assert PRIVATE not in repr(calls) and not list(tmp_path.iterdir())
    repo.git("rev-parse", "HEAD")
    assert "user.name=Operator" not in calls[-1][0]


def test_web_new_settings_and_token_no_echo(panel):  # noqa: F811
    client, coordinator = panel
    data = {
        "version": "7",
        "buildx_builder": "demo-builder",
        "git_author_name": "Operator",
        "git_author_email": "op@example.test",
    }
    assert post(client, "choices", data).status_code == 303
    saved = coordinator.calls[-1][1][1]
    assert saved["buildx_builder"] == "demo-builder"
    assert saved["git_author_name"] == "Operator"
    received = []
    coordinator.save_git_token = lambda project, value: received.append((project, value))
    response = post(client, "git-token", {"value": PRIVATE})
    assert response.status_code == 303 and received == [("other", PRIVATE)]
    assert PRIVATE not in response.text
    html = client.get("/setup?project=other").text
    assert PRIVATE not in html and 'name="git_author_name"' in html
    assert "필요 도구 설치" in html and "QEMU 등록은 수행하지 않습니다" in html


def test_saved_identity_token_and_builder_context(rig):  # noqa: F811
    service, setup, _ = rig
    service.save_project_settings(
        "flaskr", {"repo_url": URL}, updated_by="operator", expected_version=0
    )
    version = service.get_project_settings("flaskr")["version"]
    setup.save_choices(
        "flaskr",
        {
            "buildx_builder": "demo-builder",
            "git_author_name": "Operator",
            "git_author_email": "op@example.test",
        },
        expected_version=version,
    )
    seen = []
    from tests.unit.test_setup_service_fix11 import Builder

    def factory(root, **kwargs):
        seen.append(kwargs)
        b = Builder(root)
        b.builder_name = kwargs["builder_name"]
        return b

    setup._build_factory = factory
    assert setup.build_context("flaskr")["builder"] == "demo-builder"
    setup.save_git_token("flaskr", PRIVATE)
    view = setup.view("flaskr")
    assert view["git_token_configured"] and PRIVATE not in str(view)
    assert SecretVault(setup.vault.path).configured("flaskr", "git_push_token")
    setup.delete_git_token("flaskr")
    assert not setup.view("flaskr")["git_token_configured"]


def test_nginx_null_ready_gets_management_default():
    from ddak.onprem.deploy.registration import _defaults

    for tier in ({"kind": "nginx"}, {"kind": "nginx", "ready": None}):
        result = _defaults({"tiers": {"web": tier}})
        assert result["tiers"]["web"]["ready"] == {"port": 8080, "path": "/nginx-health"}


def test_candidate_inherits_command_identity_without_config_changes(tmp_path):
    import subprocess

    from ddak.core.candidate import tree_manifest

    def git(*args, cwd=tmp_path):
        return subprocess.check_output(
            ["git", "-c", "user.name=Seed Operator", "-c", "user.email=seed@example.test", *args],
            cwd=cwd,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()

    bare = tmp_path / "remote.git"
    git("init", "--bare", str(bare))
    checkout = tmp_path / "seed"
    git("clone", bare.as_uri(), str(checkout))
    (checkout / "app.py").write_text("VERSION = 1\n")
    git("add", "app.py", cwd=checkout)
    git("commit", "-m", "Initial release", cwd=checkout)
    source_sha = git("rev-parse", "HEAD", cwd=checkout)
    git("push", "origin", "HEAD:prod", "HEAD:ai-prod", cwd=checkout)
    repo = AppRepository(
        checkout,
        allow_local=True,
        secret_scan=lambda _: None,
        author=("Release Operator", "operator@example.test"),
    )
    before = (checkout / ".git/config").read_bytes()
    manifest = tree_manifest(repo, source_sha)
    result = repo.prepare_candidate(
        source_sha, manifest, manifest, None, tmp_path / "candidate", lambda: None
    )
    author = git(
        "show", "-s", "--format=%an <%ae>|%cn <%ce>", result["candidate_sha"], cwd=checkout
    )
    assert (
        author
        == "Release Operator <operator@example.test>|Release Operator <operator@example.test>"
    )
    assert (checkout / ".git/config").read_bytes() == before


def test_onprem_launcher_defaults_do_not_mask_saved_choices(tmp_path, monkeypatch):
    import importlib.util
    import os
    import sys
    from pathlib import Path

    import uvicorn

    for name in (
        "DDAK_BUILD_BACKEND",
        "DDAK_IMAGE_REPOSITORY",
        "DDAK_JEV_BACKEND",
        "DDAK_LLM_BACKEND",
        "DDAK_ADAPTER_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    entry = Path(__file__).resolve().parents[2] / "scripts/onprem_fullchain.py"
    spec = importlib.util.spec_from_file_location("test_admin_launcher", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(app, "create", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", [str(entry), "run"])
    module.main()
    assert dict(os.environ) == before
    cfg = calls[0]["settings"]
    assert cfg.adapter_mode is AdapterMode.REAL and cfg.build_backend == "local"
    saved = {
        "generation_provider": "replay",
        "judgment_provider": "replay",
        "build_backend": "codebuild",
        "image_repository": "org/other",
        "ai_timeout_s": 90,
    }
    effective = app._effective_project_settings(
        cfg, "flaskr", saved, SecretVault(tmp_path / "private"), cli_host="127.0.0.1"
    )
    assert effective.build_backend == "codebuild" and effective.image_repository == "org/other"
    assert effective.selected_provider("generation") == "replay" and effective.ai_timeout_s == 90


def test_configuration_notes_name_conflicting_keys_without_values(rig, monkeypatch):  # noqa: F811
    _, setup, _ = rig
    setup.save_env("flaskr", "APP_ENV", "fixture-private")
    monkeypatch.setenv("DDAK_ONPREM_INVENTORY", "/external/inventory.json")
    notes = setup._configuration_notes(
        "flaskr",
        {},
        {
            "tiers": {
                "was": {"public_env": {"APP_ENV": "onprem"}, "env_file": "/external/runtime.env"}
            }
        },
    )
    assert any("APP_ENV" in line for line in notes)
    assert any("외부 env_file" in line for line in notes)
    assert "fixture-private" not in repr(notes) and "/external" not in repr(notes)


def test_partial_build_diagnostic_visible_and_safe(rig):  # noqa: F811
    _, setup, _ = rig
    setup._record(
        "flaskr",
        "build",
        {
            "status": "blocked",
            "detail": "arm64 지원 없음",
            "checks": [
                {"label": "uv", "status": "red", "detail": "버전 불일치", "version": "0.1.0"}
            ],
            "installation": {
                "version": "8.30.1",
                "archive_sha256": "a" * 64,
                "installed_at": 123.0,
            },
        },
    )
    state = setup.view("flaskr")["build_state"]
    assert state["status"] == "red" and state["checks"][0]["detail"] == "버전 불일치"
    assert state["installation"]["version"] == "8.30.1"


def test_credential_helper_git_protocol_offline(tmp_path):
    import os
    import subprocess

    from ddak.core.git_credentials import helper_options, isolated_git_env

    vault = tmp_path / "private"
    save_token(vault, "flaskr", URL, PRIVATE)
    # 일반 앱의 동명 패키지가 제품 helper를 가리지 않아야 한다.
    (tmp_path / "ddak").mkdir()
    (tmp_path / "ddak/__init__.py").write_text("")
    options = helper_options(vault, "flaskr", URL)
    assert " -P -m " in options[1]
    completed = subprocess.run(
        [
            "git",
            *[part for option in options for part in ("-c", option)],
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=github.com\npath=fixture/app.git\n\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=isolated_git_env(dict(os.environ)),
    )
    assert completed.returncode == 0 and "password=" + PRIVATE in completed.stdout
    assert PRIVATE not in completed.stderr


def test_identity_overrides_specialized_git_settings(tmp_path, monkeypatch):
    import os
    import subprocess

    monkeypatch.setenv("GIT_CONFIG_COUNT", "4")
    for n, key in enumerate(("author.name", "author.email", "committer.name", "committer.email")):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{n}", key)
        monkeypatch.setenv(
            f"GIT_CONFIG_VALUE_{n}", "wrong@example.test" if key.endswith("email") else "Wrong"
        )
    subprocess.run(["git", "init", str(tmp_path / "repo")], check=True, capture_output=True)
    repo = AppRepository(tmp_path / "repo", author=("Operator", "op@example.test"))
    tree = repo.git("mktree")
    sha = repo.git("commit-tree", tree, "-m", "Release")
    assert (
        repo.git("show", "-s", "--format=%an|%ae|%cn|%ce", sha)
        == "Operator|op@example.test|Operator|op@example.test"
    )
    assert os.environ["GIT_CONFIG_VALUE_0"] == "Wrong"


def test_intake_and_watcher_policy_use_same_url_bound_vault(tmp_path, rig, monkeypatch):  # noqa: F811
    from ddak.plan.intake.fetch import _env
    from ddak.plan.intake.policy import FetchPolicy

    service, setup, _ = rig
    selected = app._project_fetch_policy(service, FetchPolicy(), "flaskr", URL)
    assert selected.credentials == (setup.vault.path, "flaskr", URL) and selected.token is None
    env = _env(selected, URL)
    assert "credential.helper=" not in env.values()
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper" and env["GIT_CONFIG_VALUE_0"] == ""
    assert (
        env["GIT_CONFIG_KEY_1"] == "credential.helper"
        and str(setup.vault.path) in env["GIT_CONFIG_VALUE_1"]
    )
    assert "ddak.core.git_credentials" in env["GIT_CONFIG_VALUE_1"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and "PYTHONPATH" in env
    with pytest.raises(DdakToolError):
        _env(selected, URL.replace("app.git", "other.git"))
    assert PRIVATE not in str(env)


@pytest.mark.parametrize("scope", ["include", "worktree"])
def test_included_or_worktree_auth_header_rejected(tmp_path, scope):
    import subprocess

    root = tmp_path / "repo"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    config = root / ".git/config"
    before = config.read_text()
    if scope == "include":
        extra = tmp_path / "included.config"
        extra.write_text(
            '[http "https://github.com/fixture/app.git"]\nextraHeader = fixture-header\n'
        )
        config.write_text(before + "[include]\npath = " + str(extra) + "\n")
    else:
        config.write_text(before + "[extensions]\nworktreeConfig = true\n")
        (root / ".git/config.worktree").write_text(
            '[http "https://github.com/fixture/app.git"]\nextraHeader = fixture-header\n'
        )
    configured = config.read_bytes()
    repo = AppRepository(root, credentials=(tmp_path / "private", "flaskr", URL))
    with pytest.raises(DdakToolError, match="별도 HTTP 인증 헤더"):
        repo.require_origin(URL)
    assert config.read_bytes() == configured
