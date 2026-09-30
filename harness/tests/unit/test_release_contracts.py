"""O1 릴리스 계약. 미구현 모델은 수집 오류 대신 명시적인 assertion으로 실패한다."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from ddak.core.contracts.context import RunContext
from tests.support import REPO_ROOT, load_script

SOURCE = "sha256:" + "a" * 64
BUILD = "sha256:" + "b" * 64
PATCH = "sha256:" + "c" * 64
INDEX = "sha256:" + "d" * 64
AMD64 = "sha256:" + "e" * 64
ARM64 = "sha256:" + "f" * 64
BAD_HASHES = (
    "a" * 64,
    "sha256:" + "A" * 64,
    "sha256:" + "g" * 64,
    "sha256:" + "a" * 63,
    "sha256:" + "a" * 65,
    SOURCE + "\n",
)


def _model(name: str) -> Any:
    path = REPO_ROOT / "src/ddak/core/contracts/release.py"
    assert path.is_file(), "release.py 릴리스 계약이 아직 구현되지 않았다"
    model = getattr(importlib.import_module("ddak.core.contracts.release"), name, None)
    assert isinstance(model, type), f"릴리스 계약 {name}이 아직 구현되지 않았다"
    return model


def _snapshot() -> dict[str, str]:
    return {
        "source_snapshot_hash": SOURCE,
        "build_snapshot_hash": BUILD,
        "patch_sha256": PATCH,
    }


def _image() -> dict[str, Any]:
    return {
        "ref": f"docker.io/example/was@{INDEX}",
        "index_digest": INDEX,
        "platform_digests": {"linux/amd64": AMD64, "linux/arm64": ARM64},
    }


def _release() -> Any:
    return _model("ReleaseArtifacts")(
        snapshot=_snapshot(), images={"was": _image()}, observations={}
    )


def test_snapshot_requires_equal_source_and_build_only_without_patch() -> None:
    model = _model("SnapshotBinding")
    unchanged = model(source_snapshot_hash=SOURCE, build_snapshot_hash=SOURCE)
    assert unchanged.patch_sha256 is None
    assert model(**_snapshot()).build_snapshot_hash == BUILD
    with pytest.raises(ValidationError):
        model(source_snapshot_hash=SOURCE, build_snapshot_hash=BUILD)


@pytest.mark.parametrize("field", ["source_snapshot_hash", "build_snapshot_hash", "patch_sha256"])
def test_snapshot_hashes_require_prefixed_lowercase_sha256(field: str) -> None:
    model = _model("SnapshotBinding")
    for bad_hash in BAD_HASHES:
        with pytest.raises(ValidationError):
            model(**{**_snapshot(), field: bad_hash})


def test_image_ref_binds_to_index_and_requires_both_platforms() -> None:
    model = _model("ImageArtifact")
    assert model(**_image()).index_digest == INDEX
    for ref in ("docker.io/example/was:latest", f"docker.io/example/was@{AMD64}"):
        with pytest.raises(ValidationError):
            model(**{**_image(), "ref": ref})
    for missing in ("linux/amd64", "linux/arm64"):
        payload = _image()
        del payload["platform_digests"][missing]
        with pytest.raises(ValidationError):
            model(**payload)


def test_image_and_observation_digest_patterns_are_validated() -> None:
    artifact = _model("ImageArtifact")
    observation = _model("ImageObservation")
    for bad_hash in BAD_HASHES:
        # ref도 같은 값으로 바꿔 index 불일치 검사만으로 통과하는 잘못된 RED를 막는다.
        with pytest.raises(ValidationError):
            artifact(**{**_image(), "index_digest": bad_hash, "ref": f"repo@{bad_hash}"})
        for platform in ("linux/amd64", "linux/arm64"):
            payload = _image()
            payload["platform_digests"][platform] = bad_hash
            with pytest.raises(ValidationError):
                artifact(**payload)
            with pytest.raises(ValidationError):
                observation(platform=platform, platform_digest=bad_hash)


def test_observations_match_the_tier_and_actual_platform_digest() -> None:
    model = _model("ReleaseArtifacts")
    payload = {
        "snapshot": _snapshot(),
        "images": {"was": _image()},
        "observations": {
            "local": {"was": {"platform": "linux/arm64", "platform_digest": ARM64}},
            "cloud": {"was": {"platform": "linux/amd64", "platform_digest": AMD64}},
        },
    }
    release = model(**payload)
    assert release.observations["local"]["was"].platform_digest == ARM64
    assert release.observations["cloud"]["was"].platform_digest == AMD64
    assert _release().observations == {}  # 관측 전 빌드 산출물도 표현할 수 있다.
    bad_observations = (
        {"local": {"was": {"platform": "linux/arm64", "platform_digest": AMD64}}},
        {"cloud": {"was": {"platform": "linux/amd64", "platform_digest": ARM64}}},
        {"local": {"was": {"platform": "linux/amd64", "platform_digest": INDEX}}},
        {"local": {"other": {"platform": "linux/amd64", "platform_digest": AMD64}}},
        {"local": {"was": {"platform": "windows/amd64", "platform_digest": AMD64}}},
        {"staging": {"was": {"platform": "linux/amd64", "platform_digest": AMD64}}},
    )
    for observations in bad_observations:
        with pytest.raises(ValidationError):
            model(**{**payload, "observations": observations})


def test_run_context_preserves_legacy_images_and_serializes_release_artifacts() -> None:
    legacy = RunContext("run-old", images={"was": "legacy-ref"})
    assert hasattr(legacy, "release_artifacts"), "RunContext.release_artifacts가 필요하다"
    assert legacy.release_artifacts is None
    assert json.loads(json.dumps(legacy.to_json_dict()))["images"] == {"was": "legacy-ref"}
    release = _release()
    for images in ({}, {"was": _image()["ref"]}, {"web": "legacy-web-ref"}):
        context = RunContext("run-new", images=images, release_artifacts=release)
        data = json.loads(json.dumps(context.to_json_dict()))
        assert data["images"] == images
        assert data["release_artifacts"]["snapshot"]["patch_sha256"] == PATCH
        assert data["release_artifacts"]["images"]["was"]["ref"] == _image()["ref"]
        assert data["adapter_mode"] == "fake"
        assert data["mode"] == "update"


def test_run_context_rejects_conflicting_refs_for_the_same_tier() -> None:
    release = _release()
    with pytest.raises(ValueError):
        RunContext(
            "run-conflict",
            images={"was": f"docker.io/example/was@{SOURCE}"},
            release_artifacts=release,
        ).to_json_dict()


def test_exporter_registers_and_generates_every_new_contract_schema() -> None:
    export = load_script("export_schemas")
    expected = {
        ("ddak.core.contracts.release", "ReleaseArtifacts"),
        ("ddak.core.contracts.approval", "ApprovalRecord"),
    }
    entries = {(module, name): filename for filename, module, name in export.TOP_LEVEL}
    assert expected <= entries.keys(), "새 계약 모델을 스키마 exporter 목록에 등록해야 한다"
    files = export.expected_files()
    for key in expected:
        schema = json.loads(files[entries[key]])
        assert schema["type"] == "object"
        assert schema["properties"]
        assert schema["additionalProperties"] is False
    release_schema = json.loads(files[entries[("ddak.core.contracts.release", "ReleaseArtifacts")]])
    assert {"SnapshotBinding", "ImageArtifact", "ImageObservation"} <= release_schema[
        "$defs"
    ].keys()
