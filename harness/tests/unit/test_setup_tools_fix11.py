"""Docker/다운로드를 실행하지 않는 초기 준비 및 local 경로 연결 검사."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from ddak.cloud.build import local
from ddak.core import setup_tools as setup
from ddak.core.contracts.errors import DdakToolError
from ddak.core.setup_tools import BuildSetup, DockerConfig


@pytest.fixture(autouse=True)
def deny_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("실제 프로세스/네트워크 호출 금지")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(setup.urllib.request, "build_opener", forbidden)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.created = False
        self.failure: str | None = None
        self.arm64 = "linux/arm64/v8"
        self.version = "8.30.1"
        self.uv_version = "uv 0.12.20 (fixture)"
        self.image: str | None = None
        self.username = "test-team"

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input: str | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {"argv": argv, "cwd": cwd, "env": dict(env), "input": input, "timeout": timeout}
        )
        if self.failure == "exception":
            raise RuntimeError("fake-" + "private-output")
        if self.failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout, output="fake-" + "private-output")
        if self.failure == "nonzero":
            return subprocess.CompletedProcess(
                argv, 1, "fake-private-output", "fake-private-output"
            )
        output = ""
        if argv[-1] == "version" and Path(argv[0]).name == "gitleaks":
            output = self.version
        elif argv == ["uv", "--version"]:
            output = self.uv_version
        elif argv[:3] == ["docker", "system", "info"]:
            output = "Server:\n Username: " + self.username + "\n"
        elif argv[:3] == ["docker", "buildx", "version"]:
            output = "github.com/docker/buildx v0.31.0 fake-sha"
        elif argv[:3] == ["docker", "buildx", "create"]:
            self.created = True
            self.image = argv[-1].removeprefix("image=")
        elif argv[:3] == ["docker", "buildx", "inspect"]:
            if not self.created:
                return subprocess.CompletedProcess(argv, 1, "", "not ready")
            name = argv[3] if len(argv) > 3 else env.get("BUILDX_BUILDER", "test-builder")
            output = (
                f"Name: {name}\nDriver: docker-container\nNodes:\nName: {name}0\n"
                f'Driver Options: image="{self.image}"\nStatus: running\n'
                f"Platforms: linux/amd64, {self.arm64}\n"
            )
        elif "login" in argv:
            initial = json.loads((cwd / "config.json").read_text())
            assert initial.get("auths")
            assert not initial.get("credsStore") and not initial.get("credHelpers")
            # 실제 Docker를 부르지 않고 제품 config 출력만 흉내낸다.
            config = Path(env["DOCKER_CONFIG"]) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "auths": {
                            "https://index.docker.io/v1/": {
                                "auth": base64.b64encode(
                                    (
                                        argv[argv.index("--username") + 1]
                                        + ":"
                                        + (input or "").rstrip("\n")
                                    ).encode()
                                ).decode()
                            }
                        }
                    }
                )
            )
            config.chmod(0o644)
            output = "fake-private-output"
        return subprocess.CompletedProcess(argv, 0, output, "fake-private-output")


def tar_payload(entries: list[tuple[str, bytes]], *, symlink: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "/outside"
                archive.addfile(info)
            else:
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeDownloader:
    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.calls: list[dict] = []

    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> bytes:
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes})
        assert url.startswith(setup._RELEASE) and url.endswith(".tar.gz")
        return self.archive


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    archive = tar_payload(
        [("gitleaks", b"fake executable"), ("LICENSE", b"license"), ("README.md", b"readme")]
    )
    archive_sha = hashlib.sha256(archive).hexdigest()
    monkeypatch.setitem(setup._GITLEAKS_DIGESTS, "linux_x64", archive_sha)
    runner = FakeRunner()
    downloader = FakeDownloader(archive)
    helper = BuildSetup(tmp_path / "setup" / "project", runner=runner, downloader=downloader)
    return helper, runner, downloader


def apply(helper: BuildSetup) -> dict:
    plan = helper.plan()
    helper.approve(plan["hash"])
    return helper.apply(plan["hash"])


def test_plan_public_deterministic_package_pins_and_no_side_effects(prepared) -> None:
    helper, runner, downloader = prepared
    one = helper.plan()
    two = helper.plan()
    assert one == two
    unhashed = {key: value for key, value in one.items() if key != "hash"}
    assert (
        one["hash"]
        == hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    spec = yaml.safe_load(files("ddak").joinpath("cloud/build/buildspec.yml").read_text())
    pre = spec["phases"]["pre_build"]["commands"]
    assert one["digests"]["buildkit"] in pre[3]
    assert one["actions"][1]["argv"][:3] == ["docker", "buildx", "create"]
    assert "--use" not in one["actions"][1]["argv"]
    assert "binfmt" not in one["digests"] and set(one["downloads"]) == {"archive"}
    assert helper.docker_config == helper.root / "docker"
    assert one["paths"] == {
        "docker_config": str(helper.docker_config),
        "builder": helper.builder_name,
        "tool_dir": str(helper.tool_dir),
    }
    assert not runner.calls and not downloader.calls and not helper.root.exists()
    one["actions"][1]["argv"][0] = "mutation"
    assert helper.plan() == two


def test_official_release_digests_are_fixed() -> None:
    assert setup.GITLEAKS_VERSION == "8.30.1"
    assert setup._GITLEAKS_DIGESTS == {
        "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
        "darwin_x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
        "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
        "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    }


def test_apply_requires_current_exact_explicit_approval(prepared) -> None:
    helper, runner, downloader = prepared
    plan = helper.plan()
    with pytest.raises(DdakToolError):
        helper.apply(plan["hash"])
    with pytest.raises(DdakToolError):
        helper.approve("0" * 64)
    helper.approve(plan["hash"])
    with pytest.raises(DdakToolError):
        helper.apply("0" * 64)
    with pytest.raises(DdakToolError):
        helper.apply(plan["hash"])
    assert not helper.root.exists() and not runner.calls and not downloader.calls


def test_buildspec_drift_invalidates_approval(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    helper, runner, downloader = prepared
    plan = helper.plan()
    helper.approve(plan["hash"])
    original = setup._pins()
    monkeypatch.setattr(setup, "_pins", lambda: (original[0], "f" * 64))
    with pytest.raises(DdakToolError):
        helper.apply(plan["hash"])
    assert not helper.root.exists() and not runner.calls and not downloader.calls


def test_apply_install_probe_paths_cleanup_and_repeat_read_only(prepared, capsys, caplog) -> None:
    helper, runner, downloader = prepared
    result = apply(helper)
    assert result["status"] == "ready" and all(result["measured"].values())
    assert result["credentials_verified"] is False and result["warnings"]
    assert (helper.tool_dir / "gitleaks").read_bytes() == b"fake executable"
    assert (helper.tool_dir / "gitleaks").stat().st_mode & 0o777 == 0o700
    assert not (helper.tool_dir / "README.md").exists()
    assert not list(helper.root.rglob(".pending-*"))
    assert all(0 < call["timeout"] <= 60 for call in runner.calls)
    assert all(call["env"]["DOCKER_CONFIG"] == str(helper.docker_config) for call in runner.calls)
    assert all(call["env"]["BUILDX_BUILDER"] == helper.builder_name for call in runner.calls)
    assert not capsys.readouterr().out and not caplog.text
    runner.calls.clear()
    downloader.calls.clear()
    before = {p: p.stat().st_mtime_ns for p in helper.root.rglob("*")}
    assert helper.probe()["ready"]
    assert apply(helper)["ready"]
    assert not downloader.calls
    assert all(
        not {"create", "run", "login", "--bootstrap"}.intersection(c["argv"]) for c in runner.calls
    )
    assert before == {p: p.stat().st_mtime_ns for p in helper.root.rglob("*")}


def test_unconfigured_probe_only_checks_uv_without_creating_files(prepared) -> None:
    helper, runner, downloader = prepared
    state = helper.probe()
    assert state["status"] == "blocked" and not state["ready"]
    assert state["measured"]["uv"]
    assert not any(value for key, value in state["measured"].items() if key != "uv")
    assert not helper.root.exists() and not downloader.calls
    assert [call["argv"] for call in runner.calls] == [["uv", "--version"]]


def test_bad_sha_prevents_install_and_mutation_commands(prepared) -> None:
    helper, runner, downloader = prepared
    downloader.archive += b"corrupt"
    with pytest.raises(DdakToolError):
        apply(helper)
    assert not (helper.tool_dir / "gitleaks").exists()
    assert not any("run" in call["argv"] or "create" in call["argv"] for call in runner.calls)
    assert not list(helper.root.rglob(".pending-*"))


@pytest.mark.parametrize(
    "names",
    [
        ["../gitleaks"],
        ["/gitleaks"],
        ["nested/gitleaks"],
        ["gitleaks", "unexpected"],
        ["gitleaks", "gitleaks"],
        [],
    ],
)
def test_unsafe_tar_names_rejected(names: list[str]) -> None:
    archive = tar_payload([(name, b"fake") for name in names])
    with pytest.raises(DdakToolError):
        setup._binary(archive, "archive.tar.gz")


def test_tar_link_rejected_and_zip_single_binary() -> None:
    with pytest.raises(DdakToolError):
        setup._binary(tar_payload([("gitleaks", b"")], symlink=True), "archive.tar.gz")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("gitleaks", b"fake binary")
        archive.writestr("LICENSE", b"license")
    assert setup._binary(buffer.getvalue(), "archive.zip") == b"fake binary"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../gitleaks", b"fake binary")
    with pytest.raises(DdakToolError):
        setup._binary(buffer.getvalue(), "archive.zip")


@pytest.mark.parametrize("failure", ["nonzero", "exception", "timeout"])
def test_failure_outputs_never_surface(prepared, failure: str, capsys, caplog) -> None:
    helper, runner, _downloader = prepared
    runner.failure = failure
    with pytest.raises(DdakToolError) as error:
        apply(helper)
    assert "fake-private-output" not in str(error.value)
    assert "fake-private-output" not in caplog.text + capsys.readouterr().out
    assert helper.probe()["status"] == "blocked"


def test_green_requires_measured_pin_version_and_real_platforms(prepared) -> None:
    helper, runner, _downloader = prepared
    apply(helper)
    runner.arm64 = "linux/arm64*"
    assert not helper.probe()["ready"]
    runner.arm64 = "linux/arm64/v8"
    runner.image = "moby/buildkit@sha256:" + "0" * 64
    assert not helper.probe()["ready"]
    runner.image = helper.plan()["digests"]["buildkit"]
    runner.version = "8.30.0"
    assert not helper.probe()["ready"]
    runner.version = "8.30.1"
    (helper.tool_dir / "gitleaks").write_bytes(b"tampered")
    assert not helper.probe()["ready"]


def test_product_login_token_only_stdin_and_private_file(tmp_path: Path, capsys, caplog) -> None:
    runner = FakeRunner()
    config = DockerConfig(tmp_path / "product-docker", runner=runner)
    assert config.status() == {"configured": False, "verified": False}
    value = "fake-" + "login-value"
    result = config.login("test-team", value)
    assert result["configured"] and result["login_confirmed"]
    assert result["status"] == "green" and result["verified"] is False
    call = runner.calls[-1]
    assert call["input"] == value + "\n" and value not in repr(call["argv"])
    assert "--password-stdin" in call["argv"] and call["timeout"] == 30
    assert call["env"]["DOCKER_CONFIG"] == str(config.path)
    assert "HOME" not in call["env"]
    assert (config.path / "config.json").stat().st_mode & 0o777 == 0o600
    assert config.path.stat().st_mode & 0o777 == 0o700
    assert value not in repr(result) + repr(config) + caplog.text + capsys.readouterr().out


@pytest.mark.parametrize("failure", ["exception", "timeout", "nonzero"])
def test_login_sanitizes_errors(tmp_path: Path, failure: str) -> None:
    runner = FakeRunner()
    runner.failure = failure
    config = DockerConfig(tmp_path / "docker", runner=runner)
    with pytest.raises(DdakToolError) as error:
        config.login("test-team", "fake-" + "login-value")
    assert "fake-private-output" not in str(error.value)
    assert "fake-login-value" not in str(error.value)


def test_login_rejects_symlink_helpers_and_invalid_input_before_runner(tmp_path: Path) -> None:
    runner = FakeRunner()
    root = tmp_path / "docker"
    root.mkdir(mode=0o700)
    config = DockerConfig(root, runner=runner)
    for username, token in [("--bad", "fake"), ("test-team", "a\nb"), ("test-team", "")]:
        with pytest.raises(DdakToolError):
            config.login(username, token)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (root / "config.json").symlink_to(outside)
    with pytest.raises(DdakToolError):
        config.login("test-team", "fake-value")
    assert outside.read_text() == "untouched"
    (root / "config.json").unlink()
    (root / "config.json").write_text('{"credsStore":"external"}')
    (root / "config.json").chmod(0o600)
    with pytest.raises(DdakToolError):
        config.login("test-team", "fake-value")
    assert not runner.calls


def test_preflight_config_env_isolated_builder_and_warning_retained(prepared) -> None:
    helper, runner, _downloader = prepared
    apply(helper)
    runner.username = ""
    runner.calls.clear()
    warnings = local.preflight_local_build(
        "test-team/app", runner=runner, config=helper.plan()["paths"]
    )
    assert warnings and "로그인 확인 불가" in warnings[0]
    for call in runner.calls:
        assert call["env"]["DOCKER_CONFIG"] == str(helper.docker_config)
        assert call["env"]["BUILDX_BUILDER"] == helper.builder_name
        assert call["env"]["PATH"].split(":")[0] == str(helper.tool_dir)
        assert not {"run", "create", "login", "--bootstrap"}.intersection(call["argv"])
    runner.arm64 = "linux/arm64*"
    with pytest.raises(DdakToolError):
        local.preflight_local_build("test-team/app", runner=runner, config=helper.plan()["paths"])


def test_ctx_platform_local_paths_used_and_config_invalid_blocks(prepared) -> None:
    helper, runner, _downloader = prepared
    apply(helper)
    local.configure_local_build(runner=runner, config=helper.plan()["paths"])
    try:
        env = local._environment(helper.plan()["paths"])
        assert env["BUILDX_BUILDER"] == helper.builder_name
        assert env["DOCKER_CONFIG"] == str(helper.docker_config)
        assert local._environment()["DOCKER_CONFIG"] == str(helper.docker_config)
        with pytest.raises(DdakToolError):
            local._environment({"builder": "partial"})
        with pytest.raises(DdakToolError):
            local._environment({**helper.plan()["paths"], "builder": "--invalid"})
        link = helper.root / "link"
        link.symlink_to(helper.docker_config, target_is_directory=True)
        with pytest.raises(DdakToolError):
            local._environment({**helper.plan()["paths"], "docker_config": str(link)})
    finally:
        local.configure_local_build()


def test_runtime_context_paths_reach_actual_local_build_entry(tmp_path: Path) -> None:
    from ddak.core.private_values import private_directory
    from tests.unit.cloud.build.test_local import FakeRunner as BuildRunner
    from tests.unit.cloud.build.test_local import call, context

    helper = BuildSetup(tmp_path / "setup" / "runtime", runner=FakeRunner())
    for directory in (helper.tool_dir, helper.docker_config):
        with private_directory(directory, create=True):
            pass
    DockerConfig(helper.docker_config, runner=FakeRunner())._initialize()
    runner = BuildRunner()
    runner.builder = runner.builder.replace("test-builder", helper.builder_name)
    local.configure_local_build(runner=runner)
    try:
        ctx = context(tmp_path, platform={"local_build": helper.plan()["paths"]})
        artifact = call(ctx)
        assert artifact.release_artifacts.snapshot == ctx.source_binding
        assert artifact.release_artifacts.images["web"].ref.startswith("docker.io/")
        for entry in runner.calls:
            assert entry["env"]["DOCKER_CONFIG"] == str(helper.docker_config)
            assert entry["env"]["BUILDX_BUILDER"] == helper.builder_name
            assert entry["env"]["PATH"].split(":")[0] == str(helper.tool_dir)
        assert "--bootstrap" not in runner.calls[-1]["input"]
    finally:
        local.configure_local_build()


def test_managed_scanner_without_system_path(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    from ddak.core import candidate
    from ddak.core.tool_paths import managed_tools, scanner_binary

    helper, _runner, _downloader = prepared
    apply(helper)
    monkeypatch.setenv("PATH", "")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == str(helper.tool_dir / "gitleaks")
        report = Path(argv[argv.index("--report-path") + 1])
        report.write_text("[]")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    with managed_tools(helper.tool_dir):
        assert scanner_binary() == str(helper.tool_dir / "gitleaks")
        assert candidate._scan_findings(helper.root) == set()
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["empty", "stale"])
def test_login_command_success_without_matching_saved_credentials_gray(tmp_path: Path, kind: str):
    def no_confirm(argv, *, cwd, env, input, timeout):
        if kind == "stale":
            auth = base64.b64encode(b"test-team:fake-old-value").decode()
            (cwd / "config.json").write_text(
                json.dumps({"auths": {"https://index.docker.io/v1/": {"auth": auth}}})
            )
            (cwd / "config.json").chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "ignored", "ignored")

    result = DockerConfig(tmp_path / "docker", runner=no_confirm).login(
        "test-team", "fake-new-value"
    )
    assert result["status"] == "gray" and result["login_confirmed"] is False
    assert result["verified"] is False


def test_login_generated_hardlink_rejected_before_chmod(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    outside.chmod(0o644)

    def invalid_config(argv, *, cwd, env, input, timeout):
        import os

        (cwd / "config.json").unlink()
        os.link(outside, cwd / "config.json")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(DdakToolError):
        DockerConfig(tmp_path / "docker", runner=invalid_config).login("test-team", "fake-value")
    assert outside.stat().st_mode & 0o777 == 0o644
    assert outside.read_text() == "untouched"


def test_partial_setup_retry_does_not_recreate_matching_builder(prepared) -> None:
    helper, runner, downloader = prepared
    runner.arm64 = "linux/arm64*"
    assert apply(helper)["status"] == "blocked"
    assert runner.created
    downloads = len(downloader.calls)
    runner.calls.clear()
    runner.arm64 = "linux/arm64/v8"
    assert apply(helper)["ready"]
    assert len(downloader.calls) == downloads
    assert not any("create" in call["argv"] for call in runner.calls)


def test_false_or_mismatched_builder_is_blocking_in_preflight(prepared) -> None:
    helper, runner, _downloader = prepared
    apply(helper)
    original_runner = runner

    def mismatch(argv, **kwargs):
        result = original_runner(argv, **kwargs)
        if argv[:3] == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                argv, 0, result.stdout.replace(helper.builder_name, "different-builder"), ""
            )
        return result

    with pytest.raises(DdakToolError):
        local.preflight_local_build("test-team/app", runner=mismatch, config=helper.plan()["paths"])


def test_initialize_prevents_docker_auto_helper_without_claiming_auth(tmp_path: Path) -> None:
    config = DockerConfig(tmp_path / "docker", runner=FakeRunner())
    config._initialize()
    contents = json.loads((config.path / "config.json").read_text())
    assert contents == {"auths": {"https://index.docker.io/v1/": {}}}
    assert config.status() == {"configured": False, "verified": False}
    # Docker ContainsAuth = credsStore != '' or len(credHelpers) > 0 or len(auths) > 0.
    assert len(contents["auths"]) > 0
    before = (config.path / "config.json").stat().st_mtime_ns
    config._initialize()
    assert (config.path / "config.json").stat().st_mtime_ns == before


def test_zip_symlink_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("gitleaks")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, b"/outside")
    with pytest.raises(DdakToolError):
        setup._binary(buffer.getvalue(), "archive.zip")
