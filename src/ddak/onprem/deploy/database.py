"""DB는 처음 한 번만 생성한다. 기존 컨테이너·고아 볼륨은 교체하지 않는다."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import ErrorCode
from ddak.onprem.deploy.containers import fail, image_ref
from ddak.onprem.deploy.replicas import ReplicaUnhealthy, check_ready, health_args

if TYPE_CHECKING:
    from ddak.onprem.deploy.provider import OnPremProvider


def deploy_database(provider: OnPremProvider, ctx: RunContext) -> ProviderResult:
    with provider._session("db", ctx) as (host, config):
        ref = image_ref(ctx.images.get("db"))
        if not ref.startswith(("mysql@", "docker.io/library/mysql@")):
            raise fail("DB는 digest 고정 공식 MySQL 이미지만 허용", ErrorCode.CONFIG_INVALID)
        volume = config.volumes[0]
        current = host.container(config.name)
        if current:
            host.owned(current, ctx.project, "db")
            mounts = current.get("Mounts", [])
            if not any(
                m.get("Type") == "volume"
                and m.get("Name") == volume.name
                and m.get("Destination") == "/var/lib/mysql"
                and m.get("RW") is True
                for m in mounts
            ):
                raise fail(
                    "기존 DB data volume 불일치; 수동 확인 필요", ErrorCode.PRECONDITION_FAILED
                )
            if current["Config"]["Image"] != ref:
                raise fail(
                    "기존 DB 이미지 변경 금지; 기존 digest를 유지한다",
                    ErrorCode.PRECONDITION_FAILED,
                )
            # 이미 존재하면 pull·create·start·stop·cp를 수행하지 않는다.
            observation, local = host.image(ref, config.platform)
            try:
                check_ready(host, config, current, local, observation, ref, ctx.project, "db")
            except ReplicaUnhealthy:
                raise fail(
                    "기존 DB 준비 실패; 재생성하지 않고 수동 확인", ErrorCode.PRECONDITION_FAILED
                ) from None
            return ProviderResult(
                provider=provider.name,
                function="deploy",
                changed=False,
                image_ref=ref,
                observation=observation,
                detail="기존 DB·data volume 재사용",
            )
        if ctx.mode is not RunMode.BOOTSTRAP or ctx.previous_release.get("local", {}).get(
            "images", {}
        ).get("db"):
            raise fail(
                "기존 배포 DB가 없다; 신규 생성하지 않고 수동 복구 필요",
                ErrorCode.PRECONDITION_FAILED,
            )
        # 컨테이너가 사라져도 남은 data volume을 새 DB로 오인하지 않는다.
        found = host.run(
            "volume", "ls", "-q", "--filter", f"name=^{re.escape(volume.name)}$"
        ).stdout.split()
        if volume.name in found:
            raise fail(
                "DB 컨테이너 없이 data volume이 남아 있다; 수동 복구 필요",
                ErrorCode.PRECONDITION_FAILED,
            )
        if not ctx.build_source:
            raise fail(
                "DB 초기화에는 승인된 앱 소스 사본이 필요하다", ErrorCode.PRECONDITION_FAILED
            )
        source = Path(ctx.build_source).resolve()
        assets = source / "docker" / "mysql"
        destinations = {
            "ddak.cnf": "/etc/mysql/conf.d/ddak.cnf",
            "10-init.sh": "/docker-entrypoint-initdb.d/10-ddak-init.sh",
            "init.sql.template": "/opt/ddak-init.sql.template",
            "ready.sh": "/usr/local/bin/ddak-db-ready",
        }
        for name in destinations:
            path = assets / name
            if (
                not path.is_file()
                or path.is_symlink()
                or source not in path.resolve().parents
                or path.stat().st_size > 65536
            ):
                raise fail("DB 초기화 소스 형식 오류", ErrorCode.CONFIG_INVALID)
            if stat.S_IMODE(path.stat().st_mode) != 0o644:
                raise fail("DB 초기화 파일은 모두 0644여야 한다", ErrorCode.CONFIG_INVALID)
        args = provider._args(config, ctx.project, "db")
        host.run("image", "pull", "--platform", config.platform, ref)
        observation, local = host.image(ref, config.platform)
        if (
            ctx.release_artifacts
            and observation.platform_digest
            != ctx.release_artifacts.images["db"].platform_digests[config.platform]
        ):
            raise fail("DB 빌드 산출물 digest 불일치")
        host.mutation_started = True
        host.run(
            "volume",
            "create",
            "--driver",
            "local",
            "--label",
            "ddak.managed=true",
            "--label",
            f"ddak.project={ctx.project}",
            "--label",
            "ddak.tier=db",
            volume.name,
        )
        provider._volumes(host, config, local)
        args += [
            "--label",
            f"ddak.platform={observation.platform}",
            "--label",
            f"ddak.platform-digest={observation.platform_digest}",
            "--label",
            f"ddak.config-digest={local['ConfigDigest']}",
            *health_args(config),
        ]
        for port in config.ports:
            args += ["--publish", port]
        host.run("container", "create", "--name", config.name, *args, ref)
        created = host.container(config.name)
        if not created:
            raise fail("DB 생성 상태 미확인", ErrorCode.ADAPTER_TIMEOUT)
        host.owned(created, ctx.project, "db")
        for name, destination in destinations.items():
            host.run("container", "cp", str(assets / name), f"{created['Id']}:{destination}")
        host.run("container", "start", created["Id"])
        running = host.inspect_container(created["Id"])
        try:
            check_ready(host, config, running, local, observation, ref, ctx.project, "db")
        except ReplicaUnhealthy:
            # 초기화가 진행 중일 수 있다. 실패 정리도 DB·볼륨을 삭제하지 않는다.
            raise fail("DB 초기화 준비 실패; 컨테이너·볼륨 보존, 수동 확인 필요") from None
        return ProviderResult(
            provider=provider.name,
            function="deploy",
            changed=True,
            image_ref=ref,
            observation=observation,
            detail="DB 최초 생성; data volume 보존",
        )
