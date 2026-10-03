"""patch_config 입출력. 등록 툴이 검사·성공 원장 재사용을 함께 수행한다.

계획 흐름이 부르는 계획 밖 툴이다. 토글 ON은 새 제안을, OFF는 이전 승인 패치 재사용만 허용한다.
prod 원본 스냅샷에 맞는 설정 패치와 검사 결과를 반환한다. 조립부는 이를 실행기
prepare(patch=, patch_meta=)로 연결하며 사람 승인 뒤에만 승인 트리에 들어간다.
- 패치는 UTF-8 unified diff 텍스트다(파일별 hunk 하나, core.snapshots.apply_diff 형식).
- 비밀값·줄 내용은 싣지 않는다. 위반은 코드·파일·줄 번호만 남긴다.
- prepare는 토글과 무관하게 passed=True와 실제 패치 바이트의 patch_sha256을 요구한다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ddak.core.contracts.base import AIUsage, ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Source
from ddak.core.contracts.patch_review import CodeProposal, PatchReviewRequest
from ddak.core.contracts.plan_facts import EnvKey

MAX_PATCH_CHARS = 64 * 1024  # 검사기의 패치 크기 상한(64KB)과 같다
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
RelPath = Annotated[str, Field(min_length=1, max_length=200)]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
PatternName = Literal["secret_key", "local_address", "cookie_secure", "proxy_fix"]
PatchStatus = Literal["proposed", "reused", "no_targets", "rejected"]


class ApprovedPatch(ContractModel):
    """이전 run에서 승인된 패치(재사용 후보). 실행기 승인 기록의 patch·patch_meta에서 온다."""

    patch: str = Field(min_length=1, max_length=MAX_PATCH_CHARS)
    reason: str = Field(min_length=1, max_length=200)
    source: Source = Source.LIVE


class PatchConfigInput(ToolInput):
    """source_dir은 intake가 만든 prod 원본 스냅샷(DDAK_SOURCES_DIR 기준 상대 경로)."""

    source_dir: str = Field(min_length=1, max_length=200)
    previous: ApprovedPatch | None = None
    review: PatchReviewRequest | None = None


class PatchMeta(ContractModel):
    """최상위 passed·patch_sha256과 함께 실행기 patch_meta로 연결한다."""

    reason: str = Field(min_length=1, max_length=200)
    reuse: bool
    source: Source
    gitleaks: str | None = None
    patterns: list[PatternName] = Field(default_factory=list)


class PatchViolation(ContractModel):
    code: str = Field(min_length=1, max_length=40)
    file: str = Field(default="", max_length=200)
    line: int | None = Field(default=None, ge=1)


class PatchConfigOutput(ContractModel):
    """status별 채워지는 칸.

    - proposed·reused: patch·patch_sha256·meta가 있고 passed=True. prepare에 넘긴다.
    - no_targets: 패치 없음. 성공 원장의 손실 관문을 통과한 원본으로 진행한다.
    - rejected: 새 제안 실패. patch=None, passed=False, 경고를 남긴다.
      실패한 diff를 prepare에 넘기지 않는다.
    """

    run_id: RunId
    status: PatchStatus
    passed: bool = Field(strict=True)
    patch: str | None = Field(default=None, max_length=MAX_PATCH_CHARS)
    patch_sha256: Sha256Digest | None = None
    meta: PatchMeta | None = None
    env_vars: list[EnvName] = Field(default_factory=list, max_length=10)  # 패치가 새로 읽는 키
    targets: dict[RelPath, list[PatternName]] = Field(default_factory=dict)  # 찾은 대상
    target_hashes: dict[RelPath, Sha256Digest] = Field(default_factory=dict)  # 대상 원본 해시
    violations: list[PatchViolation] = Field(default_factory=list, max_length=50)
    attempts: int = Field(default=0, ge=0, le=2)  # AI 호출 수
    source: Source | None = None  # AI 결과 출처(AI를 안 불렀으면 None)
    ai_usage: list[AIUsage] = Field(default_factory=list, max_length=2)
    env_keys: list[EnvKey] = Field(default_factory=list)
    changed_files: list[RelPath] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    proposals: list[CodeProposal] = Field(default_factory=list)
