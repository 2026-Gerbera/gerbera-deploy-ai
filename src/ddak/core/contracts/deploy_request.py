"""배포 요청(DeployRequest). receive_deploy_request의 입력(관리 페이지 폼, GitHub URL 기반).

repo_url의 허용 호스트·형식 검사는 intake의 FetchPolicy가 한다(설정 가능하므로 여기서 하지 않음).
여기서는 자격증명이 URL에 들어 있는지만 거부한다(값은 에러 메시지에 넣지 않는다).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, ConfigDict, Field, field_validator

from ddak.core.contracts.base import ContractModel
from ddak.core.contracts.enums import RunMode

__all__ = ["DeployRequest", "RepoUrl"]

_SECRET_QUERY = re.compile(r"(token|password|passwd|secret|key|auth)", re.IGNORECASE)


def _no_credentials(url: str) -> str:
    parts = urlsplit(url)
    if "@" in parts.netloc or parts.username or parts.password:
        raise ValueError("repo_url에 자격증명을 넣을 수 없다")
    if _SECRET_QUERY.search(parts.query) or _SECRET_QUERY.search(parts.fragment):
        raise ValueError("repo_url에 토큰으로 보이는 값을 넣을 수 없다")
    return url


RepoUrl = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_no_credentials)]


class DeployRequest(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)  # 에러에 입력값(URL)을 넣지 않는다

    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    repo_url: RepoUrl
    ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    target: Literal["local", "cloud", "both"]
    mode: RunMode = RunMode.UPDATE
    code_patch: bool = False
    message: str | None = Field(default=None, max_length=500)  # 표시용, 지시로 따르지 않음

    @field_validator("ref")
    @classmethod
    def _safe_ref(cls, v: str | None) -> str | None:
        if v is not None and (v.startswith("-") or ".." in v):
            raise ValueError("ref 형식이 올바르지 않다")
        return v
