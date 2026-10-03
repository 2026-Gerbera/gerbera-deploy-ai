"""사람이 지정한 프로젝트 설정. 실행 준비 때 버전과 함께 스냅샷한다."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.deploy_request import RepoUrl
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.infra_outputs import IMAGE_REPOSITORY_PATTERN

# 클라우드 쪽 프로젝트 이름 규칙(AwsSettings·IAM 경계와 같다). 소문자·숫자·하이픈만.
CLOUD_PLATFORM_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"


class ProjectSettings(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    repo_url: RepoUrl | None = None
    watch_branch: str = Field(default="prod", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    auto_detect: bool = False
    code_patch: bool = True
    default_targets: Literal["onprem", "cloud", "both"] = "onprem"
    generation_provider: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    generation_model: str | None = Field(
        default=None, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$"
    )
    judgment_provider: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    judgment_model: str | None = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
    llm_effort: Literal["low", "medium"] | None = None
    ai_answer_language: Literal["ko", "ja"] = "ko"
    ai_timeout_s: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    build_backend: Literal["codebuild", "local"] | None = None
    image_repository: str | None = Field(default=None, pattern="^" + IMAGE_REPOSITORY_PATTERN + "$")
    buildx_builder: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    git_author_name: str | None = Field(default=None, max_length=120)
    git_author_email: str | None = Field(default=None, max_length=254)
    inventory_path: str | None = Field(default=None, max_length=4096)
    cloud_domain: str | None = None
    aws_profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
    dns_mode: Literal["route53", "external"] = "external"
    hosted_zone_id: str | None = Field(default=None, pattern=r"^Z[A-Z0-9]{5,31}$")
    # 클라우드 state·리소스 이름. 비우면 프로젝트 이름(기존 동작). 온프렘·실행 기록은 프로젝트 이름.
    cloud_platform: str | None = Field(default=None, pattern=CLOUD_PLATFORM_PATTERN)

    @field_validator("git_author_name", "git_author_email")
    @classmethod
    def author_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(c) < 32 or c in "<>" for c in value):
            raise ValueError("커밋 작성자 형식 오류")
        if "[bot]" in value.lower() or re.fullmatch(
            r"claude(?: code)?|(?:openai )?codex|(?:github )?copilot|chatgpt|gemini|"
            r"ai|bot|dependabot|renovate|cursor(?: agent)?",
            value,
            re.IGNORECASE,
        ):
            raise ValueError("AI·bot 대신 사람의 커밋 작성자가 필요하다")
        return value

    @field_validator("git_author_email")
    @classmethod
    def author_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("커밋 작성자 이메일 형식 오류")
        if value is not None and re.search(
            r"@(?:anthropic|openai|cursor)\.com$|^codex@|\[bot\]|"
            r"\+copilot@users\.noreply\.github\.com$",
            value,
            re.IGNORECASE,
        ):
            raise ValueError("AI·bot 대신 사람의 커밋 작성자가 필요하다")
        return value

    @field_validator("repo_url")
    @classmethod
    def repository_url(cls, value: str | None) -> str | None:
        if value is not None:
            parts = urlsplit(value)
            _ = parts.port  # 잘못된 포트는 설정 저장 단계에서 거부한다.
            if (
                parts.scheme != "https"
                or not parts.hostname
                or not parts.path.strip("/")
                or parts.query
                or parts.fragment
                or any(c.isspace() for c in value)
            ):
                raise ValueError("자격증명 없는 HTTPS 저장소 URL이 필요하다")
        return value

    @field_validator("watch_branch")
    @classmethod
    def branch(cls, value: str) -> str:
        if any(s in value for s in ("..", "//", "@{")) or value.endswith(("/", ".", ".lock")):
            raise ValueError("감시 브랜치 형식이 잘못됐다")
        return value

    @field_validator("cloud_domain")
    @classmethod
    def domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("IP 대신 도메인이 필요하다")
        if (
            len(value) > 253
            or len(value.split(".")) < 3
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s)
                for s in value.split(".")
            )
            or any(
                value == suffix or value.endswith("." + suffix)
                for suffix in ("amazonaws.com", "cloudfront.net", "elasticbeanstalk.com")
            )
        ):
            raise ValueError("사용자 소유 서브도메인이 필요하다")
        return value

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.auto_detect and not self.repo_url:
            raise ValueError("자동 감지에는 저장소 URL이 필요하다")
        if self.dns_mode == "route53" and not self.hosted_zone_id:
            raise ValueError("Route 53 zone ID가 필요하다")
        return self


def watch_source(repo_url: str, branch: str = "prod") -> tuple[str, str]:
    """감시 중복 판정용. GitHub URL 표기와 refs/heads 별칭만 정규화한다."""
    url = urlsplit(repo_url)
    host = (url.hostname or "").lower()
    port = url.port
    authority = host if port in (None, 443) else f"{host}:{port}"
    path = url.path.rstrip("/").removesuffix(".git")
    if host == "github.com":
        path = path.lower()
    return f"{url.scheme.lower()}://{authority}{path}", branch.removeprefix("refs/heads/")


def cloud_platform_name(project: str, settings: Mapping[str, Any]) -> str:
    """클라우드 state bucket·key·생성 입력·리소스 이름에 쓰는 이름. 미지정이면 프로젝트 이름."""
    value = settings.get("cloud_platform")
    if value is None:
        return project
    if not isinstance(value, str) or not re.fullmatch(CLOUD_PLATFORM_PATTERN, value):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "클라우드 플랫폼 이름 형식 오류")
    return value
