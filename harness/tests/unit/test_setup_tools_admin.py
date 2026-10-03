"""관리자 도구 준비 계약. 모든 명령/다운로드는 가짜 응답만 사용한다."""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.request
from datetime import datetime

import pytest

from ddak.cloud.build import local
from ddak.core import setup_tools as setup
from ddak.core.contracts.errors import DdakToolError
from ddak.core.setup_tools import BuildSetup
from tests.unit.test_setup_tools_fix11 import FakeRunner, apply
from tests.unit.test_setup_tools_fix11 import deny_live_io as deny_live_io
from tests.unit.test_setup_tools_fix11 import prepared as prepared


def check(result, ident):
    return next(row for row in result["checks"] if row["id"] == ident)


def test_one_pinned_archive_versioned_path_and_public_installation(prepared):
    helper, runner, downloader = prepared
    assert helper.tool_dir == helper.root / "tools" / "gitleaks" / "8.30.1"
    assert helper.tool_dir.is_absolute()
    helper.plan()
    helper.probe()
    assert not downloader.calls
    result = apply(helper)
    assert result["ready"]
    assert [call["url"] for call in downloader.calls] == [
        setup._RELEASE + "gitleaks_8.30.1_linux_x64.tar.gz"
    ]
    record = result["installation"]
    assert set(record) == {"version", "archive_sha256", "binary_sha256", "installed_at"}
    assert record["version"] == "8.30.1"
    assert record["archive_sha256"] == hashlib.sha256(downloader.archive).hexdigest()
    assert record["binary_sha256"] == hashlib.sha256(b"fake executable").hexdigest()
    assert datetime.fromisoformat(record["installed_at"]).tzinfo is not None
    assert record == json.loads((helper.root / "setup-state.json").read_text())
    assert {row["id"] for row in result["checks"]} == set(result["measured"])
    assert all({"label", "status", "detail", "version"} <= row.keys() for row in result["checks"])
    assert "fake-private-output" not in json.dumps(result)
    assert not any(
        {"--use", "--privileged", "--install", "run"} & set(call["argv"]) for call in runner.calls
    )


def test_installation_survives_unrelated_plan_and_builder_change(prepared, monkeypatch):
    helper, runner, downloader = prepared
    installed = apply(helper)["installation"]
    before = helper.plan()["hash"]
    buildkit, _spec_hash = setup._pins()
    monkeypatch.setattr(setup, "_pins", lambda: (buildkit, "e" * 64))
    existing = BuildSetup(
        helper.root, runner=runner, downloader=downloader, builder_name="operator-builder"
    )
    assert existing.plan()["hash"] != before
    runner.calls.clear()
    downloader.calls.clear()
    result = apply(existing)
    assert result["ready"] and result["installation"] == installed
    assert not downloader.calls
    assert not any({"create", "--bootstrap", "--use"} & set(c["argv"]) for c in runner.calls)


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_builder_is_inspected_never_created_or_bootstrapped(prepared, exists):
    helper, runner, downloader = prepared
    runner.created = exists
    runner.image = "operator-chosen-image"
    helper = BuildSetup(
        helper.root, runner=runner, downloader=downloader, builder_name="existing-builder"
    )
    assert not any(action["type"] == "command" for action in helper.plan()["actions"])
    result = apply(helper)
    assert result["ready"] is exists
    assert result["installation"] is not None and result["measured"]["gitleaks"]
    assert any(
        c["argv"] == ["docker", "buildx", "inspect", "existing-builder"] for c in runner.calls
    )
    assert not any({"create", "--bootstrap", "--use", "run"} & set(c["argv"]) for c in runner.calls)


def test_explicit_name_equal_to_automatic_name_still_cannot_create(prepared):
    helper, runner, downloader = prepared
    chosen = BuildSetup(
        helper.root, runner=runner, downloader=downloader, builder_name=helper.builder_name
    )
    assert apply(chosen)["status"] == "blocked"
    assert not runner.created


@pytest.mark.parametrize("name", ["", "--use", "has space", "a\nb", "a" * 65, 1])
def test_invalid_builder_name_rejected_without_io(tmp_path, name):
    with pytest.raises(DdakToolError):
        BuildSetup(tmp_path / "setup", builder_name=name)
    assert not (tmp_path / "setup").exists()


@pytest.mark.parametrize(
    ("output", "valid", "version"),
    [
        ("uv 0.12.19", False, "0.12.19"),
        ("uv 0.12.20", True, "0.12.20"),
        ("uv 0.12.99 (fixture)", True, "0.12.99"),
        ("uv 0.13.0", False, "0.13.0"),
        ("uv 1.0.0", False, "1.0.0"),
        ("uv 0.12.20rc1", False, None),
        ("fake-private-output", False, None),
    ],
)
def test_uv_only_version_command_and_bounded_version(prepared, output, valid, version):
    helper, runner, _downloader = prepared
    runner.uv_version = output
    result = apply(helper)
    assert result["measured"]["uv"] is valid
    assert result["ready"] is valid
    assert check(result, "uv")["version"] == version
    assert {tuple(c["argv"]) for c in runner.calls if c["argv"][0] == "uv"} == {("uv", "--version")}
    assert "fake-private-output" not in json.dumps(result)


def test_uv_absent_is_reported_without_installing(prepared):
    helper, runner, downloader = prepared

    def no_uv(argv, **kwargs):
        if argv[0] == "uv":
            raise FileNotFoundError("fake-private-output")
        return runner(argv, **kwargs)

    helper = BuildSetup(helper.root, runner=no_uv, downloader=downloader)
    result = apply(helper)
    assert result["status"] == "blocked" and result["installation"] is not None
    assert "uv 없음" in check(result, "uv")["detail"]
    assert "fake-private-output" not in json.dumps(result)


def test_missing_architecture_returns_blocked_preserves_install_and_never_registers_qemu(prepared):
    helper, runner, downloader = prepared
    runner.arm64 = "linux/arm64*"
    result = apply(helper)
    assert result["status"] == "blocked" and result["measured"]["gitleaks"]
    assert "자동 등록하지 않음" in check(result, "platforms")["detail"]
    assert result["installation"] == helper.probe()["installation"]
    assert len(downloader.calls) == 1
    assert not any({"--privileged", "--install", "run"} & set(c["argv"]) for c in runner.calls)
    with pytest.raises(DdakToolError):
        helper.apply(helper.plan()["hash"])


@pytest.mark.parametrize(("system", "machine"), [("Windows", "AMD64"), ("Linux", "riscv64")])
def test_unsupported_host_has_no_install_plan_or_mutations(prepared, monkeypatch, system, machine):
    helper, runner, downloader = prepared
    monkeypatch.setattr(setup.platform, "system", lambda: system)
    monkeypatch.setattr(setup.platform, "machine", lambda: machine)
    assert helper.plan()["actions"] == [] and helper.plan()["downloads"] == {}
    result = apply(helper)
    assert result["status"] == "blocked"
    assert "지원하지 않는 플랫폼" in check(result, "gitleaks")["detail"]
    assert not helper.root.exists() and not downloader.calls
    assert [c["argv"] for c in runner.calls] == [["uv", "--version"]]


def test_partial_install_is_distinct_from_unsupported_and_never_runs_missing_binary(prepared):
    helper, runner, _downloader = prepared
    installed = apply(helper)["installation"]
    (helper.tool_dir / "gitleaks").unlink()
    runner.calls.clear()
    result = helper.probe()
    assert result["installation"] == installed
    assert "설치 불완전" in check(result, "gitleaks")["detail"]
    assert "바이너리 없음" in check(result, "gitleaks")["detail"]
    assert not any(c["argv"][0].endswith("/gitleaks") for c in runner.calls)


def test_probe_plan_failure_keeps_installation_without_exposing_error(prepared, monkeypatch):
    helper, runner, downloader = prepared
    installed = apply(helper)["installation"]
    runner.calls.clear()
    downloader.calls.clear()

    def invalid_pins():
        raise RuntimeError("fake-private-output")

    monkeypatch.setattr(setup, "_pins", invalid_pins)
    result = helper.probe()
    assert result["status"] == "blocked" and result["installation"] == installed
    assert "계획 검사 실패" in result["detail"]
    assert "fake-private-output" not in json.dumps(result)
    assert not runner.calls and not downloader.calls


def test_uv_blocked_retry_does_not_bootstrap_ready_builder(prepared):
    helper, runner, downloader = prepared
    runner.uv_version = "uv 0.13.0"
    installed = apply(helper)["installation"]
    runner.calls.clear()
    downloader.calls.clear()
    result = apply(helper)
    assert result["status"] == "blocked" and result["installation"] == installed
    assert not downloader.calls
    assert not any({"create", "--bootstrap", "--use", "run"} & set(c["argv"]) for c in runner.calls)


@pytest.mark.parametrize("field", ["version", "archive_sha256", "binary_sha256", "installed_at"])
def test_untrusted_installation_fields_never_echo_or_execute(prepared, field):
    helper, runner, _downloader = prepared
    record = apply(helper)["installation"]
    record[field] = "fake-private-output"
    (helper.root / "setup-state.json").write_text(json.dumps(record))
    runner.calls.clear()
    result = helper.probe()
    assert result["installation"] is None and not result["measured"]["gitleaks"]
    assert "fake-private-output" not in json.dumps(result)
    assert not any(c["argv"][0].endswith("/gitleaks") for c in runner.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [("version", "8.30.0"), ("archive_sha256", "0" * 64), ("binary_sha256", "0" * 64)],
)
def test_installation_requires_version_archive_and_binary_match(prepared, field, value):
    helper, runner, _downloader = prepared
    record = apply(helper)["installation"]
    record[field] = value
    (helper.root / "setup-state.json").write_text(json.dumps(record))
    runner.calls.clear()
    result = helper.probe()
    assert not result["measured"]["gitleaks"]
    assert "불일치" in check(result, "gitleaks")["detail"]
    assert not any(c["argv"][0].endswith("/gitleaks") for c in runner.calls)


def test_setup_and_local_build_share_connection_environment_without_display(prepared, monkeypatch):
    helper, runner, _downloader = prepared
    monkeypatch.setenv("DOCKER_HOST", "tcp://private-fixture:2376")
    monkeypatch.setenv("DOCKER_CONTEXT", "private-fixture-context")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/private-fixture/certificates")
    monkeypatch.setenv("BASH_ENV", "/do-not-inherit")
    monkeypatch.setenv("DDAK_PRIVATE", "do-not-inherit")
    result = apply(helper)
    assert helper._env() == local._environment(helper.plan()["paths"])
    assert all(c["env"]["DOCKER_CONTEXT"] == "private-fixture-context" for c in runner.calls)
    assert "BASH_ENV" not in helper._env() and "DDAK_PRIVATE" not in helper._env()
    assert "private-fixture" not in json.dumps(result) + json.dumps(helper.plan())


@pytest.mark.parametrize("failure", ["nonzero", "exception", "timeout"])
def test_mutating_command_failure_keeps_install_record_and_hides_output(prepared, failure):
    helper, runner, downloader = prepared

    def fail_create(argv, **kwargs):
        if argv[:3] == ["docker", "buildx", "create"]:
            if failure == "exception":
                raise RuntimeError("fake-private-output")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, output="fake-private-output")
            return subprocess.CompletedProcess(
                argv, 1, "fake-private-output", "fake-private-output"
            )
        return runner(argv, **kwargs)

    helper = BuildSetup(helper.root, runner=fail_create, downloader=downloader)
    with pytest.raises(DdakToolError) as error:
        apply(helper)
    assert "fake-private-output" not in str(error.value)
    result = helper.probe()
    assert result["installation"] is not None and result["measured"]["gitleaks"]
    with pytest.raises(DdakToolError):
        helper.apply(helper.plan()["hash"])


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/gitleaks.tar.gz",
        setup._RELEASE + "gitleaks_8.30.1_checksums.txt",
        setup._RELEASE + "../other.tar.gz",
        setup._RELEASE + "gitleaks_8.30.1_linux_x64.tar.gz?source=other",
        setup._RELEASE.replace("https:", "http:") + "gitleaks_8.30.1_linux_x64.tar.gz",
    ],
)
def test_download_only_allows_pinned_official_archive_urls(url):
    with pytest.raises(DdakToolError):
        setup._download(url, timeout=1, max_bytes=10)


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.org/archive",
        "http://release-assets.githubusercontent.com/archive",
        "https://github.com/other/archive",
        "https://release-assets.githubusercontent.com.example.org/archive",
        "https://user@release-assets.githubusercontent.com/archive",
        "https://release-assets.githubusercontent.com:8443/archive",
    ],
)
def test_unofficial_redirect_rejected_before_following(destination):
    request = urllib.request.Request(  # noqa: S310 -- URL 객체만 생성, HTTP 호출 없음
        setup._RELEASE + "gitleaks_8.30.1_linux_x64.tar.gz"
    )
    with pytest.raises(DdakToolError):
        setup._ReleaseRedirect().redirect_request(request, None, 302, "Found", {}, destination)


@pytest.mark.parametrize(
    "destination",
    [
        setup._RELEASE + "gitleaks_8.30.1_linux_x64.tar.gz",
        "https://release-assets.githubusercontent.com/github-production-release-asset/fixture",
        "https://objects.githubusercontent.com/github-production-release-asset/fixture",
    ],
)
def test_official_asset_redirect_uses_fake_http_response_only(monkeypatch, destination):
    start = setup._RELEASE + "gitleaks_8.30.1_linux_x64.tar.gz"
    original = urllib.request.Request(start)  # noqa: S310 -- 고정 HTTPS URL, HTTP 호출 없음
    request = setup._ReleaseRedirect().redirect_request(
        original, None, 302, "Found", {}, destination
    )
    assert request.full_url == destination
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return destination

        def read(self, limit):
            assert limit == 11
            return b"fixture"

    class FakeOpener:
        def open(self, url, *, timeout):
            calls.append((url, timeout))
            return FakeResponse()

    def opener(handler):
        assert isinstance(handler, setup._ReleaseRedirect)
        return FakeOpener()

    monkeypatch.setattr(setup.urllib.request, "build_opener", opener)
    assert setup._download(start, timeout=1, max_bytes=10) == b"fixture"
    assert calls == [(start, 1)]


def test_relative_root_still_produces_absolute_versioned_tool_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    helper = BuildSetup("project", runner=FakeRunner())
    assert helper.tool_dir == tmp_path / "project/tools/gitleaks/8.30.1"
