"""실행 컨텍스트(RunContext). 실행기(코드)만 채우고 툴은 읽기만 한다(✅ 장부 6).

- 이미지 digest, 도메인, 시크릿 위치, 서버 IP는 여기로만 흐른다.
  plan.json·툴 파라미터·AI 출력에는 없다.
- cloud_domain은 사람이 관리 페이지에 입력한 값을 run 시작 때 스냅샷한 것이다(✅ 장부 17).
- platform은 온프렘 인벤토리(platform.onprem.yaml)와 Terraform 출력(platform.cloud.json)에서 읽는다.
- 재생·디버깅용으로 var/runs/<run_id>/context.json에 redact 후 저장한다(ddak.core.runlog). 💭 [I-16]

공유 필드는 계약 문서와 O1 연결 가이드를 따른다. 실행 중 갱신은 DeploymentService가 담당한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ddak.core.config import AdapterMode
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import ErrorCode
from ddak.core.contracts.infra_outputs import IMAGE_REPOSITORY_PATTERN
from ddak.core.contracts.release import ReleaseArtifacts, SnapshotBinding


@dataclass(frozen=True)
class RunContext:
    run_id: str
    adapter_mode: AdapterMode = AdapterMode.FAKE
    deploy_config: Mapping[str, Any] = field(default_factory=dict)
    project: str = ""
    mode: RunMode = RunMode.UPDATE
    lock_token: str | None = None  # acquire_deploy_lock(실행기 내장)이 발급
    cloud_domain: str | None = None  # 관리 페이지 설정 스냅샷(사람 입력). AI는 쓰지 못한다
    images: Mapping[str, str] = field(default_factory=dict)  # tier -> repo@sha256:... (manifest)
    toggles: Mapping[str, bool] = field(default_factory=lambda: {"code_patch": False})
    platform: Mapping[str, Any] = field(default_factory=dict)  # 인벤토리·Terraform 출력
    release_artifacts: ReleaseArtifacts | None = None
    deadline: float | None = None  # monotonic 절대 시각, 툴/자식 프로세스 제한 시간
    previous_release: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    review_baseline_hash: str | None = None  # 제안 재계획이 관측한 환경 상태. 실행 전까지 결합한다.
    build_source: str | None = None  # 승인 후 생성한 빌드 사본 경로

    targets: Literal["onprem", "cloud", "both"] | None = None  # None은 기존 계획 대상 유지
    trigger: Literal["manual", "auto"] = "manual"

    source_sha: str | None = None  # 이번 run의 원본 prod 커밋
    candidate_sha: str | None = None  # 실제 빌드·배포한 ai-prod 커밋

    project_settings: Mapping[str, Any] = field(default_factory=dict)  # 승인 시 설정 스냅샷
    repo_url: str | None = None  # 요청한 앱 저장소(자격증명 없는 URL)
    ref: str | None = None  # 요청한 감시 브랜치. 실제 소스는 source_sha로 고정한다
    source_binding: SnapshotBinding | None = None  # 승인 뒤 실행기만 주입한다.
    build_backend: Literal["codebuild", "local"] = "codebuild"
    image_repository: str | None = None  # local 빌드 저장소, 승인 스냅샷에 결합
    preparation_failures: Mapping[str, list[str]] = field(default_factory=dict)
    preparation_errors: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    preparation_warnings: list[str] = field(default_factory=list)  # 비밀값 없는 준비 경고
    required_env_keys: tuple[str, ...] = ()  # 이름만. 필수 값은 private env에 보관한다.
    source_checks: Mapping[str, Any] = field(default_factory=dict)  # 비밀값 없는 검사·예외 요약

    def __post_init__(self) -> None:
        if self.review_baseline_hash is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.review_baseline_hash
        ):
            raise ValueError("재검토 배포 기준 해시 형식 오류")
        if not isinstance(self.preparation_warnings, list) or len(self.preparation_warnings) > 20:
            raise ValueError("준비 경고 형식 오류")
        if any(not isinstance(w, str) or len(w) > 1000 for w in self.preparation_warnings):
            raise ValueError("준비 경고 형식 오류")
        for target, error in self.preparation_errors.items():
            if (
                target != "cloud"
                or set(error) != {"phase", "code", "detail"}
                or error.get("phase") != "infra"
                or not all(isinstance(v, str) for v in error.values())
                or len(error.get("detail", "")) > 1000
            ):
                raise ValueError("인프라 준비 오류 형식 오류")
            ErrorCode(error["code"])
        if self.build_backend not in ("codebuild", "local"):
            raise ValueError("build_backend는 codebuild/local만 허용한다")
        if self.image_repository is not None and not re.fullmatch(
            IMAGE_REPOSITORY_PATTERN, self.image_repository
        ):
            raise ValueError("이미지 저장소 형식 오류")
        if any(
            t not in ("local", "cloud") or not tools or not all(isinstance(n, str) for n in tools)
            for t, tools in self.preparation_failures.items()
        ):
            raise ValueError("트랙 준비 실패 형식 오류")
        for value in (self.source_sha, self.candidate_sha):
            if value is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                raise ValueError("완전한 Git 커밋 SHA가 필요하다")
        if self.candidate_sha is not None and self.source_sha is None:
            raise ValueError("candidate_sha에는 source_sha가 필요하다")
        if self.targets not in (None, "onprem", "cloud", "both"):
            raise ValueError("targets는 onprem/cloud/both만 허용한다")
        if self.trigger not in ("manual", "auto"):
            raise ValueError("trigger는 manual/auto만 허용한다")
        if self.release_artifacts is not None:
            for tier, artifact in self.release_artifacts.images.items():
                if tier in self.images and self.images[tier] != artifact.ref:
                    raise ValueError(f"images와 release_artifacts의 참조가 다르다: {tier}")

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.review_baseline_hash is None:
            data.pop("review_baseline_hash")  # 확장 전 승인 자료의 context 해시도 유지한다.
        data["adapter_mode"] = self.adapter_mode.value
        data["mode"] = self.mode.value
        if self.release_artifacts is not None:
            data["release_artifacts"] = self.release_artifacts.model_dump(mode="json")
        if self.source_binding is not None:
            data["source_binding"] = self.source_binding.model_dump(mode="json")
        return data
