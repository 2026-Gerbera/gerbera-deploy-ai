"""사람이 지정한 프로젝트 설정. 실행 준비 때 버전과 함께 스냅샷한다."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.deploy_request import RepoUrl


class ProjectSettings(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    repo_url: RepoUrl | None = None
    watch_branch: str = Field(default="prod", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    auto_detect: bool = False
    default_targets: Literal["onprem", "cloud", "both"] = "both"
    cloud_domain: str | None = None
    dns_mode: Literal["route53", "external"] = "external"
    hosted_zone_id: str | None = Field(default=None, pattern=r"^Z[A-Z0-9]{5,31}$")

    @field_validator("repo_url")
    @classmethod
    def repository_url(cls, value: str | None) -> str | None:
        if value is not None:
            parts = urlsplit(value)
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
