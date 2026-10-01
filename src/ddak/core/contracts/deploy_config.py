"""deploy.yaml 모델(DeployConfig). 프로젝트 저장소 루트에 사람이 쓴다(AI가 만들지 않는다).

도메인·호스트·digest·시크릿 값은 넣지 않는다. 모든 경로는 저장소 루트 상대 경로다.
필드는 O2 초안이며 팀 협의 전이다.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from ddak.core.contracts.base import ContractModel, TierName

__all__ = ["DeployConfig", "TierConfig"]

_DRIVE = re.compile(r"^[A-Za-z]:")


def _check_rel(path: str) -> str:
    if not path or path.startswith(("/", "\\")) or _DRIVE.match(path) or "\\" in path:
        raise ValueError("경로는 저장소 루트 상대 경로여야 한다")
    if ".." in path.split("/"):
        raise ValueError("경로에 '..'을 쓸 수 없다")
    return path


class TierConfig(ContractModel):
    paths: tuple[str, ...] = (".",)  # tier 빌드 입력 경로. 변경 탐지 기준
    dockerfile: str | None = None  # None = Dockerfile 없음(generate_dockerfile 대상)

    @field_validator("paths")
    @classmethod
    def _paths(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("paths는 비울 수 없다")
        for p in v:
            _check_rel(p)
        return v

    @field_validator("dockerfile")
    @classmethod
    def _dockerfile(cls, v: str | None) -> str | None:
        return v if v is None else _check_rel(v)


class DeployConfig(ContractModel):
    tiers: dict[TierName, TierConfig] = Field(min_length=1)
    migrations_dir: str | None = None
    env_example: str | None = None  # 환경 키 이름 목록 파일(값 없음)

    @field_validator("migrations_dir", "env_example")
    @classmethod
    def _rel(cls, v: str | None) -> str | None:
        return v if v is None else _check_rel(v)
