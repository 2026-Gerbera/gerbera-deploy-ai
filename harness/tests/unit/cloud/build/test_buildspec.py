"""플랫폼 buildspec이 codebuild.py와 같은 약속을 지키는지 검사한다(CodeBuild 실행 없음)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import ddak.cloud.build as build_pkg
from ddak.cloud.build.codebuild import ENV_RELEASE_ID, ENV_REVISION, ENV_TIERS, exported_names

BUILDSPEC = Path(build_pkg.__file__).parent / "buildspec.yml"
TIERS = ("web", "was")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load(BUILDSPEC.read_text(encoding="utf-8"))


def _commands(spec: dict[str, Any]) -> list[str]:
    return [c for phase in spec["phases"].values() for c in phase["commands"]]


def test_exported_variables_match_codebuild_reader(spec: dict[str, Any]) -> None:
    expected: list[str] = []
    for tier in TIERS:
        index, platforms = exported_names(tier)
        expected += [index, *platforms.values()]
    assert sorted(spec["env"]["exported-variables"]) == sorted(expected)


def test_only_push_token_secret_is_read(spec: dict[str, Any]) -> None:
    refs = set(spec["env"]["secrets-manager"].values())
    assert {r.split(":")[0] for r in refs} == {"ddak-platform/dockerhub-push"}
    assert "parameter-store" not in spec["env"]
    assert "variables" not in spec["env"]  # 값은 프로젝트 env와 override로만 받는다


def test_overrides_are_validated_before_use(spec: dict[str, Any]) -> None:
    first = spec["phases"]["pre_build"]["commands"][0]
    for name in (ENV_TIERS, ENV_RELEASE_ID, ENV_REVISION, "IMAGE_REPO"):
        assert name in first
    assert "형식 오류" in first


def test_build_is_multi_arch_and_pushes_by_tag(spec: dict[str, Any]) -> None:
    text = "\n".join(_commands(spec))
    assert "--platform linux/amd64,linux/arm64" in text
    assert "--provenance=false" in text
    assert "--password-stdin" in text
    assert "buildspec" not in text  # 소스의 buildspec을 읽지 않는다


def test_image_is_labeled_with_source_revision(spec: dict[str, Any]) -> None:
    text = "\n".join(_commands(spec))
    assert f'--label "org.opencontainers.image.revision=${ENV_REVISION}"' in text


def test_privileged_helper_images_are_pinned_by_digest(spec: dict[str, Any]) -> None:
    text = "\n".join(_commands(spec))
    for image in ("tonistiigi/binfmt", "moby/buildkit"):
        assert re.search(re.escape(image) + r"@sha256:[0-9a-f]{64}\b", text), image
        assert not re.search(re.escape(image) + r"(?![@\w/-])", text), (
            image
        )  # 태그 없이 쓰지 않는다


def test_registry_cache_uses_separate_cache_tag(spec: dict[str, Any]) -> None:
    text = "\n".join(_commands(spec))
    assert '--cache-from "type=registry,ref=$IMAGE_REPO:cache-$t"' in text
    assert '--cache-to "type=registry,ref=$IMAGE_REPO:cache-$t,mode=max"' in text


def test_commands_are_valid_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash 없음")
    spec = yaml.safe_load(BUILDSPEC.read_text(encoding="utf-8"))
    for command in _commands(spec):
        result = subprocess.run([bash, "-n"], input=command, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
