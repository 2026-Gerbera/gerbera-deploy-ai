"""승인된 WAS 이미지의 C-09 마이그레이션 실행과 결과 검증."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import DockerHost, fail, image_ref

if TYPE_CHECKING:
    from ddak.onprem.deploy.provider import OnPremProvider

_MIGRATION = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Fingerprint(_Config):
    version: str = Field(max_length=256)
    sql_mode: str = Field(max_length=512)
    collation: str = Field(max_length=128)
    time_zone: str = Field(max_length=64)
    ssl_version: str = Field(max_length=64)


class _MigrationResult(_Config):
    phase: Literal["precheck", "up", "verify"]
    ok: bool
    current: Annotated[str, Field(pattern=_MIGRATION)] | None
    expected: Annotated[str, Field(pattern=_MIGRATION)]
    applied: list[Annotated[str, Field(pattern=_MIGRATION)]]
    signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fingerprint: _Fingerprint


def migrate_db(
    provider: OnPremProvider, migrations: Sequence[str], ctx: RunContext
) -> ProviderResult:
    if any(not re.fullmatch(_MIGRATION, item) for item in migrations):
        raise fail("마이그레이션 ID 오류", ErrorCode.CONFIG_INVALID)
    with provider._session("was", ctx) as (host, config):
        ref = image_ref(ctx.images.get("was", ""))
        args = provider._args(config, ctx.project, "was")
        host.run("image", "pull", "--platform", config.platform, ref)
        observation, local_image = host.image(ref, config.platform)
        provider._volumes(host, config, local_image)
        phases = []
        name = config.name + "-ddak-migrate"
        # wait 타임아웃 뒤에도 container 정리를 위해 전체 deadline 내 5초를 예약한다.
        work = DockerHost(
            host.runner,
            deadline=host.deadline - 5,
            endpoint=host.endpoint,
            local_registry=host.local_registry,
        )
        for phase in ("precheck", "up", "verify"):
            leftover = host.container(name)
            if leftover:
                host.remove(leftover, ctx.project, "was")
            try:
                host.mutation_started = True
                work.run(
                    "container",
                    "create",
                    "--name",
                    name,
                    *args,
                    "--entrypoint",
                    "python",
                    ref,
                    "-m",
                    "flaskr.migrate",
                    phase,
                    "--json",
                )
                created = work.container(name)
                if not created or not work.matches(created, local_image, observation):
                    raise fail("마이그레이션 컨테이너 이미지 불일치")
                work.owned(created, ctx.project, "was")
                work.run("container", "start", name)
                status = work.run("container", "wait", name).stdout.strip()
                if status != "0":
                    raise fail("마이그레이션 프로세스 실패")
                raw = work.run("container", "logs", name).stdout
                lines = [
                    line[len("MIGRATE_RESULT ") :]
                    for line in raw.splitlines()
                    if line.startswith("MIGRATE_RESULT ")
                ]
                if len(lines) > 1:
                    raise fail("MIGRATE_RESULT 중복")
                try:
                    result = _MigrationResult.model_validate_json(lines[0] if lines else raw)
                except ValidationError:
                    raise fail("MIGRATE_RESULT 형식 오류") from None
                if result.phase != phase or not result.ok:
                    raise fail("마이그레이션 단계 실패")
                if phase == "verify" and (
                    result.current != result.expected
                    or (migrations and result.expected != migrations[-1])
                ):
                    raise fail("요청한 마이그레이션 적용 확인 실패")
                phases.append(result.model_dump())
            finally:
                # 절대 deadline이 지나면 새 명령은 실행하지 않는다. 잔여 컨테이너는 다음 호출이
                # 소유 라벨을 검사한 뒤 제거한다. 정리 실패도 성공으로 숨기지 않는다.
                leftover = host.container(name)
                if leftover:
                    host.remove(leftover, ctx.project, "was")
        return ProviderResult(
            provider=provider.name,
            function="migrate_db",
            changed=any(p["applied"] for p in phases if p["phase"] == "up"),
            image_ref=ref,
            migration={**phases[-1], "event": "MIGRATE_RESULT", "phases": phases},
        )
