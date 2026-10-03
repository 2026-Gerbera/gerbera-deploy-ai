"""patch_config 입출력(초안, 담당 O3, 정준우 승인 대기 NEEDS_CONTEXT).

계획 흐름(plan/flow.py)이 부른다(카탈로그상 계획 밖 툴). 토글 code_patch가 켜졌으면 새 제안까지,
꺼졌어도 이전 승인 패치(previous)가 있으면 AI 없이 재적용만 한다(10/3 결정 12의 7).
prod 원본 스냅샷에 맞는 설정 패치를 제안하고, 실행기 prepare(patch=, patch_meta=)에 그대로 넘길
모양으로 돌려준다. 패치는 제안일 뿐이고 사람 승인 뒤에만 승인 트리에 들어간다.
- 패치는 UTF-8 unified diff 텍스트다(파일별 hunk 하나, core.snapshots.apply_diff 형식).
- 비밀값·줄 내용은 싣지 않는다. 위반은 코드·파일·줄 번호만 남긴다.
- 💭 prepare가 검사 결과(passed·patch_sha256)를 요구할지와 그 모양은 정준우 확인 대기다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ddak.core.contracts.base import AIUsage, ContractModel, RunId, ToolInput
from ddak.core.contracts.enums import Source

MAX_PATCH_CHARS = 64 * 1024  # 검사기의 패치 크기 상한(64KB)과 같다
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
RelPath = Annotated[str, Field(min_length=1, max_length=200)]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
PatternName = Literal["secret_key", "local_address", "cookie_secure", "proxy_fix"]
PatchStatus = Literal["proposed", "reused", "no_targets", "rejected", "patch_lost"]


class ApprovedPatch(ContractModel):
    """이전 run에서 승인된 패치(재사용 후보). 실행기 승인 기록의 patch·patch_meta에서 온다."""

    patch: str = Field(min_length=1, max_length=MAX_PATCH_CHARS)
    reason: str = Field(min_length=1, max_length=200)
    source: Source = Source.LIVE


class PatchConfigInput(ToolInput):
    """source_dir은 intake가 만든 prod 원본 스냅샷(DDAK_SOURCES_DIR 기준 상대 경로)."""

    source_dir: str = Field(min_length=1, max_length=200)
    previous: ApprovedPatch | None = None


class PatchMeta(ContractModel):
    """prepare(patch_meta=)에 그대로 넘긴다. 실행기 approval_meta의 패치 메타와 같은 모양."""

    reason: str = Field(min_length=1, max_length=200)
    reuse: bool
    source: Source


class PatchViolation(ContractModel):
    code: str = Field(min_length=1, max_length=40)
    file: str = Field(default="", max_length=200)
    line: int | None = Field(default=None, ge=1)


class PatchConfigOutput(ContractModel):
    """status별 채워지는 칸.

    - proposed·reused: patch·patch_sha256·meta가 있고 passed=True. prepare에 넘긴다.
    - no_targets: 패치 없음(고칠 줄이 없거나 AI가 없다고 답함). prod 그대로 진행한다.
    - rejected: 두 번 다 검사 불합격. patch는 마지막 제안(승인 화면 참고용), passed=False.
      prepare에 넘기지 않는다.
    - patch_lost(10/3 결정 12): 이전 승인 패치가 지운 값 줄이 새 결과에 다시 나타난다(원본이 바뀌어
      재적용하지 못했고 토글 OFF이거나 AI도 고치지 못함). passed=False, violations에 code
      "patch_lost"와 파일. 호출한 쪽은 승인 전에 run을 멈춘다.
      이전 패치를 조용히 빼고 배포하지 않는다.
    토글 OFF여도 이전 패치(previous)가 있으면 AI 없이 파일 단위 재적용만 한다(reused/patch_lost).
    """

    run_id: RunId
    status: PatchStatus
    passed: bool
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
