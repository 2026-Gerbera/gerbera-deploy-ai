"""O1 Docker provider. local health는 O1, 기능 smoke는 O3가 담당한다.

실행기가 등록 툴에서 이 provider를 호출한다. 인벤토리 계약(값/시크릿은 넣지 않는다)::

    ctx.platform['onprem'] = {
        'docker_host': 'unix:///var/run/docker.sock',  # 생략 시 현재 local Docker context
        'tiers': {
            'was': {
                'name': 'demo-was', 'platform': 'linux/arm64',
                'ports': ['127.0.0.1:8080:8000'], 'network': 'bridge',
                'volumes': [{'name': 'demo-data', 'target': '/data', 'read_only': False}],
                'env_file': '/private/runtime/demo/was.env',
                'public_env': {'APP_BASE_URL': 'http://localhost:8080', 'DB_HOST': 'db'},
            },
        },
    }

platform은 linux/amd64 또는 linux/arm64. ports/volumes는 생략 가능하다. 추가 키와
임의 Docker 플래그는 거부한다. named volume은 local driver/빈 Options만 허용한다.
VM은 mode=vm + pinned ssh 인벤토리로 연결한다. TCP daemon은 지원하지 않는다.
VM에서도 --env-file은 노트북 파일이다. Docker 인증은 노트북 credential helper를 쓴다.
ctx.images[tier]는 repo@sha256:...; ctx.deadline은 monotonic 절대 시각(없으면 180초).
롤백은 ctx.previous_release['local']['images'][tier]를 사용한다. 이전 기록이 없으면
해당 tier의 소유 컨테이너를 제거한다(최초 배포 복구). 누락된 images/tier는 거부한다.
설정·named volume은 유지하고 이미지 실행만 복구한다. DB 역마이그레이션은 하지 않는다.

inject_config는 public_env의 허용된 공개 설정을 upsert하고 SECRET_KEY 요청이 있을 때만
was.env에 token_hex(32)로 생성/재사용한다. 다른 요청 키는
운영자가 준비한 같은 파일에 있어야 한다. 파일 내용은 컨텍스트/반환/로그로 보내지 않는다.
config_ref만 반환하므로 실행기는 이 경로를 비밀값으로 오인해 파일 내용으로 바꾸면 안 된다.

마이그레이션: WAS 이미지에서 python -m flaskr.migrate {precheck,up,verify} --json.
stdout은 JSON 객체 하나 또는 'MIGRATE_RESULT ' 뒤 JSON 객체 하나를 포함해야 한다::

    {'phase': 'verify', 'ok': True, 'current': '0002', 'expected': '0002',
     'applied': ['0002'], 'signature': 'sha256:<64hex>',
     'fingerprint': {'version': '8.4', 'sql_mode': '', 'collation': 'utf8mb4_0900_ai_ci',
                     'time_zone': 'UTC', 'ssl_version': ''}}

기존 문서 C-09 후보 필드만 허용한다. verify에서 current==expected 및 요청 목록의 마지막
버전==expected를 확인한다. O3 실제 러너와의 통합 합의/검증은 별도다. 임의 stdout/stderr는
결과에 포함하지 않는다. migration은 최종 C-09 결과 + event='MIGRATE_RESULT' + phases 목록.
CLI 시간 초과는 daemon 작업 취소를 보장하지 않으며 ADAPTER_TIMEOUT은 상태 불확실이다.
배포 성공은 running/digest와 replica HTTP 준비 확인까지다. 기능 smoke는 O3가 수행한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy import config, health, migrate, replicas
from ddak.onprem.deploy.config import _private_env
from ddak.onprem.deploy.containers import (
    NAME,
    DockerHost,
    Runner,
    fail,
    image_ref,
    subprocess_runner,
)
from ddak.onprem.deploy.ssh import SSHConfig, session


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Volume(_Config):
    name: str
    target: str
    read_only: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not NAME.fullmatch(value):
            raise ValueError("named volume 필요")
        return value

    @field_validator("target")
    @classmethod
    def valid_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or value == "/" or ".." in path.parts:
            raise ValueError("volume target 오류")
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", value):
            raise ValueError("volume target 오류")
        return value


class _Ready(_Config):
    port: int = Field(default=8000, ge=1, le=65535)
    path: str = "/health/ready"
    timeout_s: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)

    @field_validator("path")
    @classmethod
    def ready_path(cls, value: str) -> str:
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]*", value) or value.startswith("//"):
            raise ValueError("ready path 오류")
        return value


class _Tier(_Config):
    name: str
    platform: Literal["linux/amd64", "linux/arm64"]
    ports: list[str] = Field(default_factory=list)
    network: str = "bridge"
    volumes: list[_Volume] = Field(default_factory=list)
    env_file: str | None = None
    public_env: dict[str, str] = Field(default_factory=dict)
    replicas: int | None = Field(default=None, ge=1, le=5)
    traefik_labels: dict[str, str] = Field(default_factory=dict)
    ready: _Ready | None = None

    @field_validator("traefik_labels")
    @classmethod
    def valid_labels(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            not re.fullmatch(r"traefik\.[a-zA-Z0-9_.-]+", key)
            or key == "traefik.docker.network"
            or any(c in value for c in "\n\r\0")
            for key, value in values.items()
        ):
            raise ValueError("Traefik 라벨 오류")
        return values

    @model_validator(mode="after")
    def replicas_ports(self) -> _Tier:
        if self.replicas and self.replicas > 1 and self.ports:
            raise ValueError("복수 replica는 publish 포트를 사용할 수 없다")
        return self

    @field_validator("public_env")
    @classmethod
    def valid_public_env(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {
            "APP_BASE_URL",
            "APP_ENV",
            "PROXY_FIX_X_FOR",
            "PROXY_FIX_X_PROTO",
            "SESSION_COOKIE_SECURE",
            "SESSION_COOKIE_HTTPONLY",
            "SESSION_COOKIE_SAMESITE",
            "DB_HOST",
            "DB_PORT",
            "DB_NAME",
        }
        if values.keys() - allowed:
            raise ValueError("공개 설정 허용 목록 밖의 키")
        for key, value in values.items():
            if not value or len(value) > 512 or any(c.isspace() for c in value) or "\0" in value:
                raise ValueError("공개 설정 값 오류")
            if key == "APP_BASE_URL":
                url = urlsplit(value)
                if (
                    url.scheme not in {"http", "https"}
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                    or "%" in value
                ):
                    raise ValueError("credential 없는 공개 URL 필요")
                if url.port is not None and not 0 < url.port <= 65535:
                    raise ValueError("URL 포트 오류")
            elif key in {"SESSION_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY"}:
                if value.lower() not in {"true", "false"}:
                    raise ValueError("공개 bool 설정 오류")
            elif key in {"PROXY_FIX_X_FOR", "PROXY_FIX_X_PROTO"}:
                if not re.fullmatch(r"[0-5]", value):
                    raise ValueError("ProxyFix hop은 0-5 정수여야 한다")
            elif key == "SESSION_COOKIE_SAMESITE":
                if value not in {"Lax", "Strict", "None"}:
                    raise ValueError("SameSite 설정 오류")
            elif key == "DB_PORT":
                if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 65535:
                    raise ValueError("DB 포트 오류")
            elif not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
                raise ValueError("공개 DB 이름/호스트 오류")
        return values

    @field_validator("name", "network")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not NAME.fullmatch(value) or value in {"host", "default"}:
            raise ValueError("컨테이너/네트워크 이름 오류")
        return value

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, values: list[str]) -> list[str]:
        for value in values:
            match = re.fullmatch(r"127\.0\.0\.1:([0-9]+):([0-9]+)(?:/tcp)?", value)
            if not match or not all(0 < int(port) <= 65535 for port in match.groups()):
                raise ValueError("loopback TCP 포트만 허용")
        return values

    @field_validator("env_file")
    @classmethod
    def valid_env_file(cls, value: str | None) -> str | None:
        if value is not None and (not Path(value).is_absolute() or "\n" in value or "\0" in value):
            raise ValueError("env 파일 절대 경로 필요")
        return value


class _Inventory(_Config):
    mode: Literal["container", "vm"] = "container"
    docker_host: str | None = None
    public_url: str | None = None
    ssh: SSHConfig | None = None
    tiers: dict[str, _Tier]

    @field_validator("public_url")
    @classmethod
    def public_address(cls, value: str | None) -> str | None:
        if value is not None:
            _Tier.valid_public_env({"APP_BASE_URL": value})
        return value

    @model_validator(mode="after")
    def mode_fields(self) -> _Inventory:
        if (
            self.public_url
            and self.public_url.startswith("http://")
            and any(
                t.public_env.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
                for t in self.tiers.values()
            )
        ):
            raise ValueError("Secure 쿠키에는 HTTPS public_url이 필요하다")
        if self.mode == "container" and self.ssh is not None:
            raise ValueError("container 모드는 ssh를 받지 않는다")
        if self.mode == "vm":
            if self.ssh is None or self.docker_host is not None:
                raise ValueError("vm은 ssh가 필요하고 docker_host를 받지 않는다")
            if any(t.ports or t.network == "bridge" for t in self.tiers.values()):
                raise ValueError("vm은 publish 없이 공유 network가 필요하다")
        return self


class OnPremProvider:
    name: ClassVar[str] = ProviderName.ONPREM.value
    target: ClassVar[Target] = Target.LOCAL

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        health_checker: Callable[[RunContext], ProviderResult] | None = None,
    ) -> None:
        self.runner = runner
        self.health_checker = health_checker

    def _runtime(self, tier: str, ctx: RunContext) -> tuple[DockerHost, _Tier]:
        if not NAME.fullmatch(ctx.project) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", tier):
            raise fail("project/tier 이름 오류", ErrorCode.CONFIG_INVALID)
        try:
            inventory = _Inventory.model_validate(ctx.platform.get("onprem"))
            config = inventory.tiers[tier]
        except (ValidationError, KeyError, TypeError):
            raise fail("onprem tier 인벤토리 오류", ErrorCode.CONFIG_INVALID) from None
        if len({v.target for v in config.volumes}) != len(config.volumes):
            raise fail("volume target 중복", ErrorCode.CONFIG_INVALID)
        if config.public_env and not config.env_file:
            raise fail("공개 설정에는 env_file이 필요하다", ErrorCode.CONFIG_INVALID)
        if inventory.mode == "vm" and config.ready is None:
            config = config.model_copy(update={"ready": _Ready()})
        host = DockerHost(
            self.runner,
            deadline=getattr(ctx, "deadline", None),
            endpoint="ssh://ddak-target" if inventory.mode == "vm" else inventory.docker_host,
            local_registry=inventory.mode == "vm",
        )
        host.check_deadline()
        return host, config

    @contextmanager
    def _session(self, tier: str, ctx: RunContext) -> Iterator[tuple[DockerHost, _Tier]]:
        host, config = self._runtime(tier, ctx)
        inventory = _Inventory.model_validate(ctx.platform["onprem"])
        try:
            if inventory.ssh:
                with session(inventory.ssh, self.runner, host.deadline) as runner:
                    host.runner = runner
                    yield host, config
            else:
                yield host, config
        except DdakToolError as error:
            if error.code == ErrorCode.ADAPTER_TIMEOUT and not host.mutation_started:
                raise fail("상태 변경 전 Docker 작업 시간 초과") from None
            raise

    @staticmethod
    def _args(config: _Tier, project: str, tier: str) -> list[str]:
        args = ["--platform", config.platform, "--network", config.network]
        for key, value in {"managed": "true", "project": project, "tier": tier}.items():
            args.extend(["--label", f"ddak.{key}={value}"])
        if config.env_file:
            _private_env(Path(config.env_file))
            args.extend(["--env-file", config.env_file])
        for volume in config.volumes:
            value = f"type=volume,src={volume.name},dst={volume.target}"
            args.extend(["--mount", value + (",readonly" if volume.read_only else "")])
        return args

    @staticmethod
    def _volumes(host: DockerHost, config: _Tier, image: Mapping[str, Any]) -> None:
        if set(image.get("Volumes") or {}) - {v.target for v in config.volumes}:
            raise fail(
                "이미지 VOLUME에는 명시적인 named volume이 필요하다", ErrorCode.CONFIG_INVALID
            )
        for volume in config.volumes:
            found = host.run(
                "volume", "ls", "-q", "--filter", f"name=^{re.escape(volume.name)}$"
            ).stdout.split()
            if volume.name not in found:
                host.run("volume", "create", "--driver", "local", volume.name)
            observed = host.json(
                "volume",
                "inspect",
                "--format",
                '{"Driver":{{json .Driver}},"Options":{{json .Options}}}',
                volume.name,
            )
            if observed.get("Driver") != "local" or observed.get("Options"):
                raise fail(
                    "bind/device volume driver 옵션은 허용하지 않는다", ErrorCode.CONFIG_INVALID
                )

    def _replace(self, tier: str, ctx: RunContext, ref: str, function: str) -> ProviderResult:
        with self._session(tier, ctx) as (host, config):
            return self._replace_on_host(host, config, tier, ctx, ref, function)

    def _replace_on_host(
        self,
        host: DockerHost,
        config: _Tier,
        tier: str,
        ctx: RunContext,
        ref: str,
        function: str,
    ) -> ProviderResult:
        return replicas.replace_replicas(self, host, config, tier, ctx, ref, function)

    def deploy(self, tier: str, ctx: RunContext) -> ProviderResult:
        return self._replace(tier, ctx, ctx.images.get(tier, ""), "deploy")

    def rollback(self, tier: str, ctx: RunContext) -> ProviderResult:
        releases = getattr(ctx, "previous_release", {})
        previous = releases.get("local")
        if previous is not None:
            if not isinstance(previous, Mapping):
                raise fail("이전 릴리스 형식 오류", ErrorCode.CONFIG_INVALID)
            images = previous.get("images")
            if not isinstance(images, Mapping) or tier not in images:
                raise fail("이전 릴리스의 tier image가 없다", ErrorCode.CONFIG_INVALID)
            if images[tier] is not None:
                return self._replace(tier, ctx, image_ref(images[tier]), "rollback")
        with self._session(tier, ctx) as (host, config):
            return self._remove_on_host(host, config, tier, ctx)

    def _remove_on_host(
        self,
        host: DockerHost,
        config: _Tier,
        tier: str,
        ctx: RunContext,
    ) -> ProviderResult:
        return replicas.remove_replicas(self, host, config, tier, ctx)

    def health_check(self, ctx: RunContext) -> ProviderResult:
        if self.health_checker:
            return self.health_checker(ctx)
        return health.health_check(self, ctx)

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        return migrate.migrate_db(self, migrations, ctx)

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        return config.inject_config(self, keys, ctx)

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        del mode, ctx
        return ProviderResult(
            provider=self.name,
            function="ensure_tls",
            applicable=False,
            detail="해당 없음: 로컬 HTTPS는 보류(⏸)",
        )
