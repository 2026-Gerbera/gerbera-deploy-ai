"""실행 컨텍스트(RunContext). 실행기(코드)만 채우고 툴은 읽기만 한다(✅ 장부 6).

- 이미지 digest, 도메인, 시크릿 위치, 서버 IP는 여기로만 흐른다.
  plan.json·툴 파라미터·AI 출력에는 없다.
- cloud_domain은 사람이 관리 페이지에 입력한 값을 run 시작 때 스냅샷한 것이다(✅ 장부 17).
- platform은 온프렘 인벤토리(platform.onprem.yaml)와 Terraform 출력(platform.cloud.json)에서 읽는다.
- 재생·디버깅용으로 var/runs/<run_id>/context.json에 redact 후 저장한다(ddak.core.runlog). 💭 [I-16]

TODO(contract): 필드 확정은 계약 문서. 하네스는 실행기 최소 예시에 필요한 모양만 둔다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ddak.core.config import AdapterMode
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.release import ReleaseArtifacts


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

    def __post_init__(self) -> None:
        if self.release_artifacts is not None:
            for tier, artifact in self.release_artifacts.images.items():
                if tier in self.images and self.images[tier] != artifact.ref:
                    raise ValueError(f"images와 release_artifacts의 참조가 다르다: {tier}")

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["adapter_mode"] = self.adapter_mode.value
        data["mode"] = self.mode.value
        if self.release_artifacts is not None:
            data["release_artifacts"] = self.release_artifacts.model_dump(mode="json")
        return data
