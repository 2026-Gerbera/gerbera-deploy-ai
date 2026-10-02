"""배포할 미빌드 tier의 환경별 digest를 승인 입력에 고정한다."""

import json
import re
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from ddak.core.config import AdapterMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan import Plan
from ddak.core.contracts.release import CarriedImageSource, ImageArtifact, SnapshotBinding
from ddak.core.snapshots import materialize


def locked_database(source: Path, snapshot: SnapshotBinding, patch: bytes | None) -> ImageArtifact:
    """기존 온프렘 CLI의 images.lock.json/mysql 계약. 승인된 수정본에서만 읽는다."""
    try:
        with tempfile.TemporaryDirectory(prefix="ddak-db-lock-") as directory:
            root = Path(directory) / "source"
            materialize(source, root, snapshot, patch)
            lock = root / "images.lock.json"
            if lock.stat().st_size > 65536:
                raise ValueError("size")
            value = json.loads(lock.read_text())["mysql"]
            artifact = ImageArtifact.model_validate(
                {k: value[k] for k in ("ref", "index_digest", "platform_digests")}
            )
            if not artifact.ref.startswith(("mysql@", "docker.io/library/mysql@")):
                raise ValueError("official mysql only")
            return artifact
    except (OSError, ValueError, KeyError, TypeError):
        raise DdakToolError(
            ErrorCode.CONFIG_INVALID,
            "DB 배포: images.lock.json에 공식 mysql 및 두 플랫폼 digest가 필요하다",
        ) from None


def carried_image_source(previous: Mapping[str, Any], tier: str, target: str) -> CarriedImageSource:
    """기존 릴리스도 읽되, provider에 전달할 출처는 계약 모델로 검증한다."""
    try:
        origin = (previous.get("image_sources") or {}).get(tier)
        if origin is None:
            artifacts = previous.get("artifacts") or {}
            origin = {
                "artifact": artifacts.get("images", {}).get(tier),
                "release_id": previous.get("release_id"),
                "source_sha": previous.get("source_sha"),
                "candidate_sha": previous.get("candidate_sha"),
                "snapshot": previous.get("source"),
                "observation": artifacts.get("observations", {}).get(target, {}).get(tier),
            }
        source = CarriedImageSource.model_validate({**origin, "carried_forward": True})
        if source.artifact.ref != previous["images"][tier]:
            raise ValueError("ref mismatch")
        return source
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED,
            f"{target}/{tier}: 이월 이미지 산출물 또는 digest가 잘못됐다; tier 빌드 필요",
        ) from None


def carried_images(
    plan: Plan,
    previous: Mapping[str, Mapping[str, Any]],
    mode: AdapterMode,
    supplied: Collection[str] = (),
    blocked: Collection[str] = (),
) -> dict[str, dict[str, str]]:
    built = {s.tier for s in plan.build.steps if s.tool == "build_image"} | set(supplied)
    result: dict[str, dict[str, str]] = {}
    for target in ("local", "cloud"):
        if target in blocked:
            continue
        for step in getattr(plan.deploy, target).steps:
            if step.tool != "deploy_tier" or not step.tier or step.tier in built:
                continue
            # 온프렘 DB는 provider가 공식 MySQL digest와 기존 볼륨을 관리한다.
            if target == "local" and step.tier == "db":
                continue
            old = previous.get(target, {})
            ref = old.get("images", {}).get(step.tier)
            if (
                not isinstance(ref, str)
                or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", ref)
                or old.get("source_mode") != mode.value
            ):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    f"{target}/{step.tier}: 이번 빌드와 이월할 성공 이미지가 없다; tier 빌드 필요",
                )
            carried_image_source(old, step.tier, target)
            result.setdefault(target, {})[step.tier] = ref
    return result
