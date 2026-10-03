"""관리 화면 연결 coordinator: 실제 저장소 + 가짜 외부 검사, 터미널 입력 없음."""

import pytest

from ddak import app
from ddak.core.config import Settings
from ddak.core.contracts.errors import DdakToolError
from ddak.core.registry import Registry
from ddak.core.setup_service import SetupService
from ddak.executor.service import DeploymentService


class Builder:
    calls: list[str]

    def __init__(self, root):
        self.calls = []
        self.root = root
        self.tool_dir = root / "tools"
        self.docker_config = root / "docker"
        self.builder_name = "fixture-builder"
        self.approved = None

    def plan(self):
        return {"hash": "a" * 64, "actions": [{"type": "fixture"}]}

    def approve(self, digest):
        assert digest == self.plan()["hash"]
        self.approved = digest

    def apply(self, digest):
        assert self.approved == digest
        self.calls.append("approved-setup")
        return {"status": "ready", "detail": "fixture"}

    def probe(self):
        return {"status": "ready", "detail": "fixture"}

    def login(self, username, token):
        assert username == "fixture" and token
        self.calls.append("stdin-login")
        return {"status": "green", "detail": "fixture authenticated"}


@pytest.fixture
def rig(tmp_path):
    service = DeploymentService(Registry([]), tmp_path.resolve() / "state")

    def catalog():
        return [
            {
                "id": "fake-api",
                "label": "Fake API",
                "kind": "api",
                "roles": ["generation", "judgment"],
                "auth": "api_key",
                "key_name": "fake_key",
                "default_model": "fixture",
                "models": ["fixture"],
            }
        ]

    def validate(ident, role):
        assert ident == "fake-api" and role in ("generation", "judgment")

    def inventory(root, project, data):
        import json

        path = root / "inventory.json"
        path.write_text(json.dumps(data))
        return path

    def reader(path):
        import json

        return json.loads(path.read_text())

    seen = []

    def probe(ident, cfg, *, role):
        seen.append((ident, role))
        return {"status": "green", "detail": "fixture"}

    manager = SetupService(
        service,
        catalog=catalog,
        effective=lambda project, saved, vault: Settings(
            llm_provider=saved.get("generation_provider"),
            judgment_provider=saved.get("judgment_provider"),
            build_backend=saved.get("build_backend") or "codebuild",
            image_repository=saved.get("image_repository"),
        ),
        test_provider=probe,
        validate_selection=validate,
        inventory_writer=inventory,
        inventory_reader=reader,
        build_factory=Builder,
    )
    service.onboarding = manager
    yield service, manager, seen
    service.close()


def test_settings_vault_verification_and_replacement(rig):
    service, setup, seen = rig
    setup.save_choices(
        "flaskr",
        {
            "generation_provider": "fake-api",
            "judgment_provider": "fake-api",
            "build_backend": "local",
            "image_repository": "2026gerbera/flaskr",
        },
        expected_version=0,
    )
    setup.save_key("flaskr", "fake-api", "fixture-value-private")
    view = setup.view("flaskr")
    assert view["keys"]["fake-api"]
    assert view["states"]["fake-api"]["status"] == "gray"
    assert "fixture-value-private" not in str(view)
    setup.test_provider("flaskr", "fake-api")
    assert seen == [("fake-api", "generation"), ("fake-api", "judgment")]
    assert setup.view("flaskr")["states"]["fake-api"]["status"] == "green"
    setup.save_key("flaskr", "fake-api", "replacement-private")
    assert setup.view("flaskr")["states"]["fake-api"]["status"] == "gray"
    setup.delete_key("flaskr", "fake-api")
    assert not setup.view("flaskr")["keys"]["fake-api"]
    assert "fixture-value-private" not in str(service.get_project_settings("flaskr"))


def test_build_setup_bound_approval_and_env_preserved(rig):
    _, setup, _ = rig
    setup.save_env("flaskr", "APP_ENV", "production")
    env = setup.runtime_env("flaskr")
    env.write_text(env.read_text() + "SECRET_KEY=" + "a" * 64 + "\n")
    setup.save_env("flaskr", "ANOTHER", "some-value")
    assert "SECRET_KEY=" + "a" * 64 in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    assert setup.view("flaskr")["env_keys"] == ["ANOTHER", "APP_ENV"]
    assert "some-value" not in str(setup.view("flaskr"))
    with pytest.raises(DdakToolError):
        setup.save_env("flaskr", "DATABASE_URL_MIGRATOR", "never-runtime")
    setup.apply_build("flaskr", setup.build_plan("flaskr")["hash"])
    assert setup._state("flaskr", "build")["status"] == "green"
    with pytest.raises(AssertionError):
        setup.apply_build("flaskr", "b" * 64)


def test_required_red_blocks_both_manual_and_auto_gate(rig):
    _, setup, _ = rig
    setup.save_choices("flaskr", {"generation_provider": "fake-api"}, expected_version=0)
    setup._record("flaskr", "fake-api", {"status": "red", "detail": "login required"})
    with pytest.raises(DdakToolError, match="AI"):
        setup.require_ready("flaskr", "onprem")
    setup._record("flaskr", "fake-api", {"status": "green", "detail": "fixture"})
    setup.require_ready("flaskr", "onprem")


def test_managed_backend_takes_precedence_over_explicit_environment(rig, monkeypatch):
    """관리 화면 저장값은 같은 항목의 실행 환경 기본값보다 우선한다."""
    _, setup, _ = rig
    monkeypatch.delenv("DDAK_BUILD_BACKEND", raising=False)
    saved = {
        "build_backend": "local",
        "image_repository": "2026gerbera/flaskr",
        "generation_provider": "replay",
        "judgment_provider": "replay",
    }
    cfg = app._effective_project_settings(
        Settings(), "flaskr", saved, setup.vault, cli_host="127.0.0.1"
    )
    assert cfg.build_backend == "local" and cfg.llm_provider == "replay"
    monkeypatch.setenv("DDAK_BUILD_BACKEND", "codebuild")
    cfg = app._effective_project_settings(
        Settings(), "flaskr", saved, setup.vault, cli_host="127.0.0.1"
    )
    assert cfg.build_backend == "local"


def test_managed_scanner_does_not_fall_back_to_system_path(tmp_path):
    from ddak.core.tool_paths import managed_tools, scanner_binary

    with managed_tools(tmp_path / "not-installed"):
        assert scanner_binary() == str(tmp_path / "not-installed/gitleaks")
    assert scanner_binary() == "gitleaks"
