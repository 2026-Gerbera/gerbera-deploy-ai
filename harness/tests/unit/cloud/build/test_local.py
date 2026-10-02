"""Docker/Git/셸 실실행 없이 local 제품 경로를 fake runner로 검증한다."""

from __future__ import annotations

import dataclasses
import subprocess
import time
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from ddak.cloud.build import configure_local_build, preflight_local_build
from ddak.cloud.build import image as image_module
from ddak.cloud.build import local as local_module
from ddak.cloud.build.codebuild import exported_names
from ddak.cloud.build.tools.build_image.tool import build_image
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import SnapshotBinding
from ddak.core.contracts.tools.build_image import BuildImageInput
from ddak.core.snapshots import digest_json, file_manifest

RID = "run-20261003-123456-ab12"
SHA = "a" * 40
REPOSITORY = "test-team/test-app"
DIGESTS = ["sha256:" + c * 64 for c in "123"]


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.payload = {"docker/web.Dockerfile": "FROM scratch\n", "app.py": "# approved\n"}
        self.sha = SHA
        self.info = "Client:\nServer:\n Username: test-team\n"
        self.builder = (
            "Name: test-builder\nDriver: docker-container\nNodes:\nName: test-builder0\n"
            "Status:         running\nPlatforms:      linux/amd64, linux/arm64/v8\n"
        )
        self.fail: str | None = None
        self.output: str | None = None

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
        stdout = ""
        if "clone" in argv:
            Path(argv[-1]).mkdir()
        if "checkout" in argv:
            for name, content in self.payload.items():
                path = cwd / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        if "rev-parse" in argv:
            stdout = self.sha + "\n"
        if argv == ["docker", "system", "info"]:
            stdout = self.info
        if argv == ["docker", "buildx", "inspect"]:
            stdout = self.builder
        if input is not None:
            if self.fail == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout, output="password=" + "private")
            if self.fail == "exception":
                raise RuntimeError("password=" + "private")
            if self.fail == "nonzero":
                return subprocess.CompletedProcess(
                    argv, 1, "password=" + "private", "token=" + "private"
                )
            index, platforms = exported_names(env["BUILD_TIERS"])
            stdout = (
                self.output
                if self.output is not None
                else "\n".join(
                    f"{name}={digest}"
                    for name, digest in zip([index, *platforms.values()], DIGESTS, strict=True)
                )
                + "\n"
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture(autouse=True)
def reset_runner():
    configure_local_build()
    yield
    configure_local_build()


def context(tmp_path: Path, **kwargs) -> RunContext:
    approved = tmp_path / "approved"
    approved.mkdir(exist_ok=True)
    for name, content in FakeRunner().payload.items():
        path = approved / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    digest = digest_json(file_manifest(approved))
    defaults = dict(
        build_backend="local",
        image_repository=REPOSITORY,
        source_binding=SnapshotBinding(source_snapshot_hash=digest, build_snapshot_hash=digest),
        candidate_sha=SHA,
        source_sha=SHA,
        repo_url="https://github.com/test-team/test-app",
        build_source=str(approved),
        deadline=time.monotonic() + 300,
    )
    defaults.update(kwargs)
    return RunContext(RID, **defaults)


def call(ctx: RunContext, tier: str = "web"):
    return build_image(BuildImageInput(run_id=RID, tier=tier), ctx)


def test_local_public_tool_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    monkeypatch.setattr(image_module, "_codebuild", lambda _: pytest.fail("AWS must not run"))
    monkeypatch.setenv("DOCKERHUB_TOKEN", "private")
    monkeypatch.setenv("BASH_ENV", "/secret/startup")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private")
    ctx = context(tmp_path)
    out = call(ctx)
    assert out.source is Source.FIXTURE
    assert out.candidate_sha == SHA
    assert out.build_id == "local:" + RID + ":web"
    assert out.release_artifacts.snapshot == ctx.source_binding
    artifact = out.release_artifacts.images["web"]
    assert artifact.ref == "docker.io/" + REPOSITORY + "@" + DIGESTS[0]
    assert artifact.platform_digests == dict(
        zip(["linux/amd64", "linux/arm64"], DIGESTS[1:], strict=True)
    )
    build = runner.calls[-1]
    assert build["argv"] == ["bash", "--noprofile", "--norc", "-s"]
    for name, value in {
        "BUILD_TIERS": "web",
        "RELEASE_ID": RID,
        "SOURCE_REVISION": SHA,
        "IMAGE_REPO": "docker.io/" + REPOSITORY,
        "CODEBUILD_BUILD_SUCCEEDING": "1",
        "BUILDX_BUILDER": "test-builder",
    }.items():
        assert build["env"][name] == value
    for recorded in runner.calls:
        assert 0 < recorded["timeout"] <= 300
        assert not {"DOCKERHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "BASH_ENV"} & recorded["env"].keys()
        assert "credential.helper=" in recorded["argv"] or recorded["argv"][0] != "git"
    script = build["input"]
    assert "docker login" not in script and "docker buildx create" not in script
    assert "tonistiigi" not in script and "--bootstrap" not in script
    assert '"/tmp/ddak-meta-$t.json"' not in script
    assert script.count('"$DDAK_LOCAL_META/ddak-meta-$t.json"') == 2
    assert "trap 'export CODEBUILD_BUILD_SUCCEEDING=0' ERR" in script
    assert not build["cwd"].exists()
    assert not Path(build["env"]["DDAK_LOCAL_META"]).exists()
    assert not capsys.readouterr().out


def test_phase_commands_are_current_package_original(tmp_path: Path) -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    call(context(tmp_path))
    script = runner.calls[-1]["input"]
    spec = yaml.safe_load(files("ddak.cloud.build").joinpath("buildspec.yml").read_text())
    assert spec["phases"]["pre_build"]["commands"][0] in script
    for phase in ("build", "post_build"):
        original = spec["phases"][phase]["commands"][0]
        rewritten = original.replace(
            '"/tmp/ddak-meta-$t.json"', '"$DDAK_LOCAL_META/ddak-meta-$t.json"'
        )
        assert rewritten in script


def test_same_snapshot_accumulates_and_uses_distinct_temp_paths(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.payload["docker/was.Dockerfile"] = "FROM scratch\n"
    ctx = context(tmp_path)
    (Path(ctx.build_source) / "docker/was.Dockerfile").write_text("FROM scratch\n")
    digest = digest_json(file_manifest(Path(ctx.build_source)))
    ctx = dataclasses.replace(
        ctx, source_binding=SnapshotBinding(source_snapshot_hash=digest, build_snapshot_hash=digest)
    )
    configure_local_build(runner=runner)
    first = call(ctx)
    second = call(dataclasses.replace(ctx, release_artifacts=first.release_artifacts), "was")
    assert set(second.release_artifacts.images) == {"web", "was"}
    builds = [c for c in runner.calls if c["input"]]
    assert builds[0]["env"]["DDAK_LOCAL_META"] != builds[1]["env"]["DDAK_LOCAL_META"]


@pytest.mark.parametrize(
    "repository",
    [
        None,
        "docker.io/" + REPOSITORY,
        REPOSITORY + ":tag",
        REPOSITORY + "\n",
        "docker.io/user:pass@app/x",
    ],
)
def test_local_repository_requires_approved_fullmatch(tmp_path: Path, repository) -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    ctx = context(tmp_path)
    # 생성자 검사를 우회한 입력도 제품 분기에서 다시 거부하는지 확인한다.
    object.__setattr__(ctx, "image_repository", repository)
    with pytest.raises(DdakToolError) as error:
        call(ctx)
    assert error.value.code is ErrorCode.CONFIG_INVALID
    assert not runner.calls


@pytest.mark.parametrize("failure", ["sha", "manifest", "approved_copy"])
def test_approval_mismatch_stops_before_docker(tmp_path: Path, failure: str) -> None:
    runner = FakeRunner()
    ctx = context(tmp_path)
    if failure == "sha":
        runner.sha = "b" * 40
    if failure == "manifest":
        runner.payload["app.py"] = "# changed\n"
    if failure == "approved_copy":
        Path(ctx.build_source, "app.py").write_text("# changed\n")
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError) as error:
        call(ctx)
    assert error.value.code is ErrorCode.PRECONDITION_FAILED
    assert not any(c["argv"][0] == "docker" for c in runner.calls)
    assert not runner.calls[-1]["cwd"].exists()


@pytest.mark.parametrize(
    "state",
    [
        "Client:\nServer:\n",
        "Username: <none>",
        "Username: test-team\nUsername: second-user",
        "Username: test-team\nUsername: <none>",
    ],
)
def test_login_missing_or_unverifiable_warns_and_checks_builder(state: str) -> None:
    runner = FakeRunner()
    runner.info = state
    warnings = preflight_local_build(REPOSITORY, runner=runner)
    assert len(warnings) == 1 and "로그인 확인 불가" in warnings[0]
    assert "docker login" in warnings[0]
    assert runner.calls[-1]["argv"] == ["docker", "buildx", "inspect"]
    assert not runner.calls[0]["cwd"].exists()


@pytest.mark.parametrize("username,warning", [("test-team", False), ("other-user", True)])
def test_login_namespace_mismatch_is_only_a_warning(username: str, warning: bool) -> None:
    runner = FakeRunner()
    runner.info = "Username: " + username
    warnings = preflight_local_build(REPOSITORY, runner=runner)
    assert bool(warnings) is warning
    if warning:
        assert "namespace" in warnings[0] and username not in warnings[0]


@pytest.mark.parametrize(
    "builder",
    [
        "Name: x\nDriver: docker-container\nName: n\nStatus: running\nPlatforms: linux/amd64",
        "Name: x\nDriver: docker-container\nName: n\nStatus: stopped\n"
        "Platforms: linux/amd64, linux/arm64",
        "Name: x\nDriver: docker-container\nName: n\nStatus: running\n"
        "Platforms: linux/amd64*, linux/arm64*",
    ],
)
def test_existing_builder_must_support_both_platforms(builder: str) -> None:
    runner = FakeRunner()
    runner.info = "Client:\nServer:\n"  # 로그인 경고여도 builder 차단을 약하게 하지 않는다.
    runner.builder = builder
    with pytest.raises(DdakToolError, match="Buildx"):
        preflight_local_build(REPOSITORY, runner=runner)
    assert all("--bootstrap" not in c["argv"] and "create" not in c["argv"] for c in runner.calls)


def test_public_preflight_uses_configured_runner_and_cleans() -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    assert preflight_local_build(REPOSITORY) == []
    assert not runner.calls[0]["cwd"].exists()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("timeout", ErrorCode.ADAPTER_TIMEOUT),
        ("exception", ErrorCode.ADAPTER_FAILED),
        ("nonzero", ErrorCode.ADAPTER_FAILED),
    ],
)
def test_failure_discards_secret_output_and_cleans(
    tmp_path: Path, capsys, failure: str, code: ErrorCode
) -> None:
    runner = FakeRunner()
    runner.fail = failure
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError) as error:
        call(context(tmp_path))
    assert error.value.code is code
    assert "private" not in str(error.value)
    assert error.value.__cause__ is None
    assert not runner.calls[-1]["cwd"].exists()
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "stderr,auth",
    [
        ("unauthorized: authentication required", True),
        ("denied: requested access to the resource is denied", True),
        ("insufficient_scope: authorization failed", True),
        ("push access denied", True),
        ("failed to pull ghcr.io/example/base: unauthorized: authentication required", True),
        ("failed to solve: syntax error", False),
    ],
)
def test_push_authentication_failure_has_safe_login_hint(tmp_path, capsys, stderr, auth):
    class FailedPushRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if kwargs["input"] is not None:
                return subprocess.CompletedProcess(argv, 1, "", stderr + " token=" + "private")
            return result

    runner = FailedPushRunner()
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError) as error:
        call(context(tmp_path))
    assert error.value.code is ErrorCode.ADAPTER_FAILED
    assert ("docker login 필요" in str(error.value)) is auth
    if auth:
        assert "해당 레지스트리" in str(error.value)
        assert "push 권한" not in str(error.value)  # 베이스 이미지 pull도 같은 인증 경로다.
    assert stderr not in str(error.value) and "private" not in str(error.value)
    assert "} > /dev/null\n" in runner.calls[-1]["input"]
    assert not runner.calls[-1]["cwd"].exists()
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize(
    "output",
    [
        "",
        "DDAK_WEB_INDEX=broken",
        "PASSWORD=private",
        "DDAK_WEB_INDEX=" + DIGESTS[0] + "\nDDAK_WEB_INDEX=" + DIGESTS[0],
    ],
)
def test_invalid_digest_or_env_dump_is_not_returned(tmp_path: Path, output: str) -> None:
    runner = FakeRunner()
    runner.output = output
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError):
        call(context(tmp_path))


def test_fake_without_runner_never_runs_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_module, "_subprocess_runner", lambda *a, **k: pytest.fail("real process")
    )
    with pytest.raises(DdakToolError, match="runner"):
        call(context(tmp_path))


def test_real_uses_same_path_with_injected_runner(tmp_path: Path) -> None:
    configure_local_build(runner=FakeRunner())
    assert call(context(tmp_path, adapter_mode=AdapterMode.REAL)).source is Source.LIVE


def test_expired_deadline_does_not_start_process(tmp_path: Path) -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError) as error:
        call(context(tmp_path, deadline=time.monotonic() - 1))
    assert error.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert not runner.calls


def test_secret_file_paths_rejected_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    runner.payload[".docker/config.json"] = "never read"
    configure_local_build(runner=runner)
    original = Path.read_bytes

    def guarded(path: Path):
        if path.name == "config.json":
            pytest.fail("secret file was read")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    with pytest.raises(DdakToolError, match="비밀 파일"):
        call(context(tmp_path))


def test_mismatched_previous_snapshot_rejected_before_runner(tmp_path: Path) -> None:
    runner = FakeRunner()
    configure_local_build(runner=runner)
    ctx = context(tmp_path)
    previous = call(ctx).release_artifacts
    different = SnapshotBinding(
        source_snapshot_hash="sha256:" + "f" * 64, build_snapshot_hash="sha256:" + "f" * 64
    )
    runner.calls.clear()
    with pytest.raises(DdakToolError, match="앞선 빌드"):
        call(dataclasses.replace(ctx, source_binding=different, release_artifacts=previous))
    assert not runner.calls


def test_candidate_mutated_during_preflight_rejected(tmp_path: Path) -> None:
    class MutatingRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if argv == ["docker", "buildx", "inspect"]:
                checkout = next(c["cwd"] for c in self.calls if "checkout" in c["argv"])
                (checkout / "app.py").write_text("# race\n")
            return result

    runner = MutatingRunner()
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError, match="사전 검사 중"):
        call(context(tmp_path))
    assert not any(c["input"] for c in runner.calls)


def test_executable_bit_is_part_of_approved_manifest(tmp_path: Path) -> None:
    class ExecutableRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if "checkout" in argv:
                (kwargs["cwd"] / "app.py").chmod(0o755)
            return result

    runner = ExecutableRunner()
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError, match="build_files 해시"):
        call(context(tmp_path))
    assert not any(c["argv"][0] == "docker" for c in runner.calls)


@pytest.mark.parametrize("name", [".env", "nested/.ENV", "nested/key.KEY", ".docker/config.json"])
def test_secret_paths_are_not_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    runner = FakeRunner()
    runner.payload[name] = "placeholder"
    configure_local_build(runner=runner)
    original = Path.read_bytes

    def guarded(path: Path):
        if path.name == Path(name).name:
            pytest.fail("secret contents requested")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    with pytest.raises(DdakToolError, match="비밀 파일"):
        call(context(tmp_path))


def test_ignored_template_symlink_rejected_before_read(tmp_path: Path) -> None:
    class LinkRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if "checkout" in argv:
                (kwargs["cwd"] / ".env.example").symlink_to("/unread-secret-path")
            return result

    runner = LinkRunner()
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError, match="심볼릭 링크"):
        call(context(tmp_path))


def test_preflight_docker_failure_does_not_expose_output() -> None:
    class DaemonDownRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            super().__call__(argv, **kwargs)
            return subprocess.CompletedProcess(
                argv, 1, "token=" + "private", "password=" + "private"
            )

    runner = DaemonDownRunner()
    with pytest.raises(DdakToolError) as error:
        preflight_local_build(REPOSITORY, runner=runner)
    assert "private" not in str(error.value)
    assert not runner.calls[0]["cwd"].exists()


def test_preflight_rejects_repository_before_any_command() -> None:
    runner = FakeRunner()
    with pytest.raises(DdakToolError):
        preflight_local_build(REPOSITORY + "\n", runner=runner)
    assert not runner.calls


def test_buildspec_drift_fails_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = files("ddak.cloud.build").joinpath("buildspec.yml").read_text()

    class ChangedSpec:
        def joinpath(self, name):
            return self

        def read_text(self):
            return text.replace('"/tmp/ddak-meta-$t.json"', '"/tmp/new-meta-$t.json"')

    runner = FakeRunner()
    configure_local_build(runner=runner)
    monkeypatch.setattr(local_module, "files", lambda _: ChangedSpec())
    with pytest.raises(DdakToolError, match="buildspec"):
        call(context(tmp_path))
    assert not runner.calls


def test_duplicate_platform_digests_are_not_artifacts(tmp_path: Path) -> None:
    runner = FakeRunner()
    index, platforms = exported_names("web")
    runner.output = "\n".join(f"{name}={DIGESTS[0]}" for name in [index, *platforms.values()])
    configure_local_build(runner=runner)
    with pytest.raises(DdakToolError):
        call(context(tmp_path))
