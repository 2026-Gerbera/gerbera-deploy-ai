"""build_image 툴: RunContext 값으로 build_tier를 부르는 연결. FAKE는 AWS 없음, REAL은 Stubber."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from ddak.cloud.build import image as image_module
from ddak.cloud.build.codebuild import exported_names, platform_buildspec
from ddak.cloud.build.tools.build_image.tool import build_image
from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import SnapshotBinding
from ddak.core.contracts.tools.build_image import BuildImageInput

RID = "run-20261002-054700-ab12"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO_URL = "https://github.com/gerbera-demo/flaskr"
SNAP = SnapshotBinding(
    source_snapshot_hash="sha256:" + "a" * 64, build_snapshot_hash="sha256:" + "a" * 64
)
CLOUD = {"codebuild_project_name": "ddak-build", "image_repository": "gerbera/flaskr"}


def _ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {"adapter_mode": AdapterMode.FAKE, "source_binding": SNAP}
    base.update(kw)
    return RunContext(RID, **base)


def _call(tier: str, ctx: RunContext):
    return build_image(BuildImageInput(run_id=RID, tier=tier), ctx)


def test_fake_build_returns_fixture_artifacts_without_aws() -> None:
    got = _call("web", _ctx())
    assert got.source is Source.FIXTURE
    assert got.build_id is not None
    assert got.release_artifacts.snapshot == SNAP
    assert got.release_artifacts.images["web"].ref.startswith("docker.io/ddak-fake/app@")


def test_build_steps_accumulate_through_context() -> None:
    first = _call("web", _ctx(repo_url=REPO_URL, candidate_sha=SHA, source_sha=SHA))
    second = _call(
        "was",
        _ctx(
            repo_url=REPO_URL,
            candidate_sha=SHA,
            source_sha=SHA,
            release_artifacts=first.release_artifacts,
            images={t: a.ref for t, a in first.release_artifacts.images.items()},
        ),
    )
    assert set(second.release_artifacts.images) == {"web", "was"}
    assert second.candidate_sha == SHA


def test_requires_approved_snapshot() -> None:
    with pytest.raises(DdakToolError) as err:
        _call("web", _ctx(source_binding=None))
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_real_requires_repo_and_candidate() -> None:
    ctx = _ctx(adapter_mode=AdapterMode.REAL, platform={"cloud": CLOUD})
    with pytest.raises(DdakToolError) as err:
        _call("web", ctx)
    assert err.value.code is ErrorCode.PRECONDITION_FAILED


def test_real_requires_infra_outputs() -> None:
    ctx = _ctx(adapter_mode=AdapterMode.REAL, repo_url=REPO_URL, candidate_sha=SHA, source_sha=SHA)
    with pytest.raises(DdakToolError) as err:
        _call("web", ctx)
    assert err.value.code is ErrorCode.INFRA_MISSING


def test_real_build_sends_context_values_to_codebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    client = boto3.client(
        "codebuild",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )
    monkeypatch.setattr(image_module, "_codebuild", lambda cloud, ctx: client)
    index, platforms = exported_names("web")
    digests = {"linux/amd64": "sha256:" + "2" * 64, "linux/arm64": "sha256:" + "3" * 64}
    exported = [{"name": index, "value": "sha256:" + "1" * 64}] + [
        {"name": name, "value": digests[p]} for p, name in platforms.items()
    ]
    with Stubber(client) as stub:
        stub.add_response(
            "start_build",
            {"build": {"id": "ddak-build:1"}},
            {
                "projectName": "ddak-build",
                "buildspecOverride": platform_buildspec(),
                "sourceTypeOverride": "GITHUB",
                "sourceLocationOverride": REPO_URL,  # IAM이 repo_url과 정확히 같기를 요구
                "sourceVersion": SHA,
                "environmentVariablesOverride": [
                    {"name": "BUILD_TIERS", "value": "web", "type": "PLAINTEXT"},
                    {"name": "RELEASE_ID", "value": RID, "type": "PLAINTEXT"},
                    {"name": "SOURCE_REVISION", "value": SHA, "type": "PLAINTEXT"},
                    {
                        "name": "IMAGE_REPO",
                        "value": "docker.io/gerbera/flaskr",
                        "type": "PLAINTEXT",
                    },
                ],
            },
        )
        stub.add_response(
            "batch_get_builds",
            {
                "builds": [
                    {
                        "id": "ddak-build:1",
                        "buildStatus": "SUCCEEDED",
                        "resolvedSourceVersion": SHA,
                        "exportedEnvironmentVariables": exported,
                    }
                ]
            },
            {"ids": ANY},
        )
        got = _call(
            "web",
            _ctx(
                adapter_mode=AdapterMode.REAL,
                repo_url=REPO_URL,
                candidate_sha=SHA,
                source_sha=SHA,
                platform={"cloud": CLOUD},
            ),
        )
    assert got.source is Source.LIVE
    assert got.build_id == "ddak-build:1"
    assert got.release_artifacts.images["web"].ref == "docker.io/gerbera/flaskr@sha256:" + "1" * 64


def test_tool_is_registered_in_build_module() -> None:
    from ddak.app import load_tools

    registry = load_tools()
    registered = registry.get("build_image")
    assert registered.spec.module.value == "cloud.build"
    assert registered.input_model is BuildImageInput


def test_fake_ignores_local_rehearsal_repo_and_sha256_commit() -> None:
    # 리허설은 file:// bare 저장소와 SHA-256 커밋을 쓸 수 있다. FakeCodeBuild는 소스를 받지 않는다
    sha256_commit = "c" * 64
    got = _call(
        "web",
        _ctx(
            repo_url="file:///tmp/remote.git", candidate_sha=sha256_commit, source_sha=sha256_commit
        ),
    )
    assert got.source is Source.FIXTURE
    assert len(got.candidate_sha) == 40
