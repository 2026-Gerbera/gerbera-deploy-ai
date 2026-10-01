"""운영 스크립트용 사전 점검. 자원 변경·이미지 pull은 수행하지 않는다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.redact import redact_obj
from ddak.onprem.deploy.config import _private_env
from ddak.onprem.deploy.containers import Runner, fail, subprocess_runner
from ddak.onprem.deploy.provider import OnPremProvider, _Inventory
from ddak.onprem.deploy.replicas import names


def preflight_inventory(
    inventory: dict[str, Any], *, project: str = "flaskr", runner: Runner = subprocess_runner
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str = "") -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    try:
        parsed = _Inventory.model_validate(inventory)
        if not parsed.tiers:
            raise ValueError("tiers 없음")
        provider = OnPremProvider(runner)
        ctx = RunContext("preflight", project=project, platform={"onprem": inventory})
        for tier in parsed.tiers:
            provider._runtime(tier, ctx)
        record("inventory", "ok")
    except (DdakToolError, ValidationError, ValueError, TypeError):
        record("inventory", "fail", "인벤토리 형식/설정 오류")
        return {"passed": False, "checks": checks}
    try:
        if parsed.ssh:
            parsed.ssh.check_key()
            record("ssh_key_stat", "ok")
        else:
            record("ssh_key_stat", "skip", "container 모드")
        for tier, config in parsed.tiers.items():
            if config.env_file:
                _private_env(Path(config.env_file))
                record(f"env_permissions.{tier}", "ok")
            else:
                record(f"env_permissions.{tier}", "skip", "env_file 없음")
    except DdakToolError as error:
        record("local_files", "fail", str(error))
        record("docker", "skip", "로컬 파일 점검 실패")
        return {"passed": False, "checks": redact_obj(checks)}
    first = next(iter(parsed.tiers))
    try:
        with provider._session(first, ctx) as (host, _):
            record(
                "ssh_hostkey_and_connection",
                "ok" if parsed.ssh else "skip",
                "지문 대조·BatchMode 확인" if parsed.ssh else "container 모드",
            )
            version = host.json("version", "--format", "{{json .}}")
            try:
                for side in ("Server", "Client"):
                    value = version[side].get("ApiVersion", version[side].get("APIVersion", ""))
                    api = tuple(int(x) for x in value.split("."))
                    if len(api) != 2 or api < (1, 49):
                        raise ValueError
                record("docker_api", "ok", "Server/Client API >= 1.49")
            except (AttributeError, KeyError, ValueError, TypeError):
                raise fail("Docker Server/Client API 1.49 이상 필요") from None
            for tier, config in parsed.tiers.items():
                actual = str(version["Server"].get("Os")) + "/" + str(version["Server"].get("Arch"))
                actual = actual.replace("aarch64", "arm64").replace("x86_64", "amd64")
                if actual != config.platform:
                    raise fail("Docker 플랫폼과 tier platform 불일치")
                record(f"platform.{tier}", "ok", actual)
                found = host.json(
                    "network", "inspect", "--format", "{{json .Name}}", config.network
                )
                if found != config.network:
                    raise fail("공유 network 없음")
                record(f"network.{tier}", "ok")
                for _, name in names(config):
                    for candidate in (name, name + "-ddak-next", config.name + "-ddak-migrate"):
                        state = host.container(candidate)
                        if state:
                            host.owned(state, project, tier)
                record(f"ownership.{tier}", "ok")
    except DdakToolError as error:
        record("remote_checks", "fail", str(error))
    return {"passed": all(c["status"] != "fail" for c in checks), "checks": redact_obj(checks)}
