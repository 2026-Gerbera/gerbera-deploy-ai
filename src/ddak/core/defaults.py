"""비밀 없는 제품 기본값. 경로는 실행 cwd에 의존하지 않는다."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from ddak.core.project_settings import ProjectSettings

DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "harness" / "config" / "defaults.toml"


def load_defaults() -> dict[str, Any]:
    with DEFAULTS_PATH.open("rb") as stream:
        values = tomllib.load(stream)["project"]
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
