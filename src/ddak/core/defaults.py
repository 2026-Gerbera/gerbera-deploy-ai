"""비밀 없는 제품 기본값. 경로는 실행 cwd에 의존하지 않는다."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from ddak.core.project_settings import CLOUD_PLATFORM_PATTERN, ProjectSettings

DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "harness" / "config" / "defaults.toml"


def load_defaults() -> dict[str, Any]:
    with DEFAULTS_PATH.open("rb") as stream:
        values = tomllib.load(stream)["project"]
    if "cloud_platform" in values:
        # 모든 프로젝트에 같은 플랫폼을 쓰게 되므로 프로젝트별 [cloud_platform] 표에만 둔다.
        raise ValueError("기본 파일의 cloud_platform은 [cloud_platform] 표에 프로젝트별로 둔다")
    # 저장소는 사람이 입력한다. 파일의 자동 감지 기본값은 저장소가 있을 때 적용한다.
    ProjectSettings.model_validate({**values, "auto_detect": False})
    return values


def project_values(saved: dict[str, Any]) -> dict[str, Any]:
    values = {
        **ProjectSettings().model_dump(mode="json"),
        **load_defaults(),
        **{k: v for k, v in saved.items() if k in ProjectSettings.model_fields and v is not None},
    }
    if not values["repo_url"] and saved.get("auto_detect") is not True:
        values["auto_detect"] = False
    return values


def load_aws_defaults() -> dict[str, str]:
    with DEFAULTS_PATH.open("rb") as stream:
        values = tomllib.load(stream)["aws"]
    account = values.get("expected_account_id")
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("기본 파일의 AWS 기대 계정은 12자리 숫자여야 한다")
    return {"expected_account_id": account}


def cloud_platform_default(project: str) -> str | None:
    """기본 파일 [cloud_platform] 표의 프로젝트별 플랫폼 이름. 없으면 None(프로젝트 이름)."""
    with DEFAULTS_PATH.open("rb") as stream:
        values = tomllib.load(stream).get("cloud_platform", {})
    if not isinstance(values, dict) or any(
        not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
        or not isinstance(value, str)
        or not re.fullmatch(CLOUD_PLATFORM_PATTERN, value)
        for name, value in values.items()
    ):
        raise ValueError("기본 파일의 클라우드 플랫폼 이름 형식 오류")
    return values.get(project)
