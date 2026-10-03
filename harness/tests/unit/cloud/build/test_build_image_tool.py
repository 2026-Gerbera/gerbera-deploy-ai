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


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """빌드 캐시를 테스트 임시 폴더에 둔다(실제 var/build-cache를 건드리지 않음)."""
    root = tmp_path / "build-cache"
    monkeypatch.setenv("DDAK_BUILD_CACHE_DIR", str(root))
    return root


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


# 성공 이미지 재사용(서윤 0bcb397 복원): 같은 승인 스냅샷이면 CodeBuild를 부르지 않는다.
def _real_ctx(**kw: Any) -> RunContext:
    base: dict[str, Any] = {
        "adapter_mode": AdapterMode.REAL,
        "repo_url": REPO_URL,
        "candidate_sha": SHA,
        "source_sha": SHA,
        "platform": {"cloud": CLOUD},
        "project": "flaskr",
    }
    base.update(kw)
    return _ctx(**base)


def _built_once(monkeypatch: pytest.MonkeyPatch) -> Any:
    """CodeBuild 1회 성공(Stubber). 두 번째 호출이 오면 Stubber가 실패시킨다."""
    client = boto3.client(
        "codebuild",
        region_name="ap-northeast-2",
        aws_access_key_id="test" + "ing",
        aws_secret_access_key="test" + "ing",
    )
    monkeypatch.setattr(image_module, "_codebuild", lambda cloud, ctx: client)
    index, platforms = exported_names("was")
    digests = {"linux/amd64": "sha256:" + "2" * 64, "linux/arm64": "sha256:" + "3" * 64}
    exported = [{"name": index, "value": "sha256:" + "1" * 64}] + [
        {"name": name, "value": digests[p]} for p, name in platforms.items()
    ]
    stub = Stubber(client)
    stub.add_response("start_build", {"build": {"id": "ddak-build:1"}}, None)
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
        None,
    )
    stub.activate()
    return stub


def test_same_snapshot_reuses_successful_image_without_codebuild(
    monkeypatch: pytest.MonkeyPatch, _cache_dir: Any
) -> None:
    stub = _built_once(monkeypatch)
    first = _call("was", _real_ctx())
    later = "fedcba9876543210fedcba9876543210fedcba98"  # 다음 run의 후보 커밋(트리는 같음)
    again = _call("was", _real_ctx(candidate_sha=later, source_sha=later))
    stub.assert_no_pending_responses()  # CodeBuild는 한 번만 불렸다
    assert first.source is Source.LIVE
    assert again.source is Source.CACHE
    assert again.build_id is None
    assert again.release_artifacts.images["was"] == first.release_artifacts.images["was"]
    assert again.release_artifacts.snapshot == SNAP
    saved = list((_cache_dir / "flaskr").glob("was-*.json"))
    assert len(saved) == 1
    text = saved[0].read_text()
    assert "ddak-build" not in text and "test" + "ing" not in text  # 빌드 ID·자격증명 없음


def test_other_snapshot_or_tier_builds_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _built_once(monkeypatch)
    _call("was", _real_ctx())
    other = SnapshotBinding(
        source_snapshot_hash="sha256:" + "b" * 64, build_snapshot_hash="sha256:" + "b" * 64
    )
    calls: list[str] = []
    monkeypatch.setattr(
        image_module, "_codebuild", lambda cloud, ctx: calls.append("x") or object()
    )
    with pytest.raises(Exception):  # noqa: B017 (가짜 클라이언트라 빌드 시작에서 실패)
        _call("was", _real_ctx(source_binding=other))
    with pytest.raises(Exception):  # noqa: B017
        _call("web", _real_ctx())
    assert calls == ["x", "x"]  # 스냅샷이나 tier가 다르면 CodeBuild로 간다


@pytest.mark.parametrize(
    "content", ["{not json", '{"schema": "old"}', '{"schema": "ddak.build-cache/v1"}']
)
def test_broken_cache_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, _cache_dir: Any, content: str
) -> None:
    stub = _built_once(monkeypatch)
    _call("was", _real_ctx())
    for path in (_cache_dir / "flaskr").glob("was-*.json"):
        path.write_text(content)
    calls: list[str] = []
    monkeypatch.setattr(
        image_module, "_codebuild", lambda cloud, ctx: calls.append("x") or object()
    )
    with pytest.raises(Exception):  # noqa: B017
        _call("was", _real_ctx())
    assert calls == ["x"]
    stub.assert_no_pending_responses()


def test_fake_build_does_not_use_cache(_cache_dir: Any) -> None:
    _call("web", _ctx())
    _call("web", _ctx())
    assert not _cache_dir.exists()


def test_local_backend_reuses_successful_image(
    monkeypatch: pytest.MonkeyPatch, _cache_dir: Any
) -> None:
    from ddak.cloud.build.release import ReleaseBuild
    from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts

    built: list[str] = []
    artifact = ImageArtifact(
        ref="docker.io/gerbera/flaskr@sha256:" + "4" * 64,
        index_digest="sha256:" + "4" * 64,
        platform_digests={"linux/amd64": "sha256:" + "5" * 64, "linux/arm64": "sha256:" + "6" * 64},
    )

    def fake_local(tier: str, ctx: RunContext) -> ReleaseBuild:
        built.append(tier)
        return ReleaseBuild(
            artifacts=ReleaseArtifacts(snapshot=SNAP, images={tier: artifact}),
            build_id="local-1",
            revision=SHA,
        )

    monkeypatch.setattr(image_module, "build_local_tier", fake_local)
    ctx = _real_ctx(build_backend="local", image_repository="gerbera/flaskr")
    first = _call("was", ctx)
    again = _call("was", ctx)
    assert built == ["was"]  # 두 번째는 로컬 빌드를 하지 않는다
    assert first.source is Source.LIVE
    assert again.source is Source.CACHE
    assert again.release_artifacts.images["was"] == artifact
    assert len(list((_cache_dir / "flaskr").glob("was-*.json"))) == 1


def test_cache_root_does_not_depend_on_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DDAK_BUILD_CACHE_DIR")
    path = image_module._cache_path("was", _real_ctx(), "docker.io/gerbera/flaskr")
    assert path.is_absolute()
    assert path.parent.parent == image_module.CACHE_ROOT
    assert image_module.CACHE_ROOT.parts[-3:] == ("harness", "var", "build-cache")
