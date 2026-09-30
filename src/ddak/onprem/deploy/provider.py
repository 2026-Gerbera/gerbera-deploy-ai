"""O1 로컬 Docker provider(옛 cd/providers/onprem.py). health_check는 O3에 주입하거나 TODO로 남긴다.

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
원격 SSH/TCP daemon과 원격 env 전달은 아직 지원하지 않는다.
ctx.images[tier]는 repo@sha256:...; ctx.deadline은 monotonic 절대 시각(없으면 180초).
롤백은 ctx.previous_release['local']['images'][tier]를 사용한다. 이전 기록이 없으면
해당 tier의 소유 컨테이너를 제거한다(최초 배포 복구). 누락된 images/tier는 거부한다.
설정·named volume은 유지하고 이미지 실행만 복구한다. DB 역마이그레이션은 하지 않는다.

inject_config는 public_env의 허용된 공개 설정을 upsert하고 was.env의 SECRET_KEY를
token_hex(32)로 생성/재사용한다. 다른 요청 키는
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
배포 성공은 컨테이너 running/이미지 관측까지만이며 HTTP/기능 검증은 O3가 수행한다.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ddak.cd.interface import ProviderName, ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Target
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy.containers import (
    NAME,
    DockerHost,
    Runner,
    fail,
    image_ref,
    subprocess_runner,
)

_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")
_MIGRATION = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"


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


class _Tier(_Config):
    name: str
    platform: Literal["linux/amd64", "linux/arm64"]
    ports: list[str] = Field(default_factory=list)
    network: str = "bridge"
    volumes: list[_Volume] = Field(default_factory=list)
    env_file: str | None = None
    public_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("public_env")
    @classmethod
    def valid_public_env(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {
            "APP_BASE_URL",
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
                if value.lower() not in {"true", "false", "0", "1"}:
                    raise ValueError("공개 bool 설정 오류")
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
    docker_host: str | None = None
    tiers: dict[str, _Tier]


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


def _private_env(
    path: Path,
    keys: Sequence[str] | None = None,
    public_env: Mapping[str, str] | None = None,
) -> bool:
    """0600 단일 소유 일반 파일만 허용한다. keys=None이면 내용은 읽지 않는다."""
    changed = False
    try:
        if keys is not None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if keys is None:
                raise
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(path, flags)
            else:
                changed = True
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.getuid():
                raise fail("env 파일 소유권/형식 오류", ErrorCode.CONFIG_INVALID)
            # 기다리는 파일 lock 때문에 실행 deadline을 초과하지 않는다.
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if stat.S_IMODE(meta.st_mode) != 0o600:
                if keys is None:
                    raise fail("env 파일 권한은 0600이어야 한다", ErrorCode.CONFIG_INVALID)
                os.fchmod(stream.fileno(), 0o600)
                changed = True
            if keys is None:
                return False
            if meta.st_size > 65536:
                raise fail("env 파일 크기 초과", ErrorCode.CONFIG_INVALID)
            original = stream.read()
            values: dict[str, str] = {}
            for line in original.splitlines():
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep or not _IDENTIFIER.fullmatch(key) or key in values or "\x00" in value:
                    raise fail("env 파일 형식 오류", ErrorCode.CONFIG_INVALID)
                values[key] = value
            old_values = dict(values)
            values.update(public_env or {})
            if any(key != "SECRET_KEY" and not values.get(key) for key in keys):
                raise fail("요청한 환경 키가 host env 파일에 없다", ErrorCode.CONFIG_INVALID)
            if "SECRET_KEY" in values:
                if not re.fullmatch(r"[0-9a-f]{64}", values["SECRET_KEY"]):
                    raise fail("기존 SECRET_KEY는 64자리 hex여야 한다", ErrorCode.CONFIG_INVALID)
            else:
                values["SECRET_KEY"] = secrets.token_hex(32)
            if values != old_values:
                lines = original.splitlines()
                updated = [
                    line.partition("=")[0] + "=" + values[line.partition("=")[0]]
                    if line and not line.startswith("#")
                    else line
                    for line in lines
                ]
                updated.extend(
                    f"{key}={value}" for key, value in values.items() if key not in old_values
                )
                stream.seek(0)
                stream.write("\n".join(updated) + "\n")
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                changed = True
        return changed
    except (OSError, UnicodeError):
        raise fail("host env 파일 접근 실패", ErrorCode.CONFIG_INVALID) from None


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
        host = DockerHost(
            self.runner, deadline=getattr(ctx, "deadline", None), endpoint=inventory.docker_host
        )
        host.check_deadline()
        return host, config

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
        host, config = self._runtime(tier, ctx)
        ref = image_ref(ref)
        args = self._args(config, ctx.project, tier)
        current = host.container(config.name)
        if current:
            host.owned(current, ctx.project, tier)
        # pull/manifest/config 검증과 생성이 모두 끝난 뒤에만 이전 실행을 멈춘다.
        host.run("image", "pull", "--platform", config.platform, ref)
        observation, local_image = host.image(ref, config.platform)
        artifacts = ctx.release_artifacts
        if function == "deploy" and artifacts is not None and tier in artifacts.images:
            expected = artifacts.images[tier].platform_digests[observation.platform]
            if observation.platform_digest != expected:
                raise fail("빌드 산출물과 실제 플랫폼 manifest 불일치")
        self._volumes(host, config, local_image)
        spec_hash = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        previous = current["Config"]["Image"] if current else None
        same = (
            current is not None
            and previous == ref
            and host.matches(current, local_image, observation)
            and current["Config"]["Labels"].get("ddak.spec") == spec_hash
        )
        changed = not same
        if same and current is not None:
            if not current["State"]["Running"]:
                host.run("container", "start", current["Id"])
                changed = True
        else:
            staged_name = config.name + "-ddak-next"
            staged = host.container(staged_name)
            if staged:
                host.remove(staged, ctx.project, tier)
            args.extend(["--label", f"ddak.spec={spec_hash}"])
            for port in config.ports:
                args.extend(["--publish", port])
            host.run("container", "create", "--name", staged_name, *args, ref)
            staged = host.container(staged_name)
            if not staged:
                raise fail("대체 컨테이너 생성 관측 실패")
            host.owned(staged, ctx.project, tier)
            if not host.matches(staged, local_image, observation):
                raise fail("대체 컨테이너 플랫폼 manifest 불일치")
            if current:
                host.remove(current, ctx.project, tier)
            host.run("container", "rename", staged["Id"], config.name)
            host.run("container", "start", staged["Id"])
        running = host.container(config.name)
        if not running:
            raise fail("배포 컨테이너 관측 실패")
        host.owned(running, ctx.project, tier)
        if (
            not running["State"]["Running"]
            or running["Config"]["Image"] != ref
            or not host.matches(running, local_image, observation)
        ):
            raise fail("배포 이미지 실행 관측 실패")
        return ProviderResult(
            provider=self.name,
            function=function,
            changed=changed,
            previous_image=previous,
            image_ref=ref,
            observation=observation,
        )

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
        host, config = self._runtime(tier, ctx)
        current = host.container(config.name)
        prior_ref = current["Config"]["Image"] if current else None
        if current:
            host.remove(current, ctx.project, tier)
        staged = host.container(config.name + "-ddak-next")
        if staged:
            host.remove(staged, ctx.project, tier)
        return ProviderResult(
            provider=self.name,
            function="rollback",
            changed=bool(current or staged),
            previous_image=prior_ref,
        )

    def health_check(self, ctx: RunContext) -> ProviderResult:
        if self.health_checker:
            return self.health_checker(ctx)
        raise DdakToolError(ErrorCode.INTERNAL, "미구현: TODO(O3)")

    def migrate_db(self, migrations: Sequence[str], ctx: RunContext) -> ProviderResult:
        if any(not re.fullmatch(_MIGRATION, item) for item in migrations):
            raise fail("마이그레이션 ID 오류", ErrorCode.CONFIG_INVALID)
        host, config = self._runtime("was", ctx)
        ref = image_ref(ctx.images.get("was", ""))
        args = self._args(config, ctx.project, "was")
        host.run("image", "pull", "--platform", config.platform, ref)
        observation, local_image = host.image(ref, config.platform)
        self._volumes(host, config, local_image)
        phases = []
        name = config.name + "-ddak-migrate"
        # wait 타임아웃 뒤에도 container 정리를 위해 전체 deadline 내 5초를 예약한다.
        work = DockerHost(self.runner, deadline=host.deadline - 5, endpoint=host.endpoint)
        for phase in ("precheck", "up", "verify"):
            leftover = host.container(name)
            if leftover:
                host.remove(leftover, ctx.project, "was")
            try:
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
            provider=self.name,
            function="migrate_db",
            changed=any(p["applied"] for p in phases if p["phase"] == "up"),
            image_ref=ref,
            migration={**phases[-1], "event": "MIGRATE_RESULT", "phases": phases},
        )

    def inject_config(self, keys: Sequence[str], ctx: RunContext) -> ProviderResult:
        host, config = self._runtime("was", ctx)
        wanted = sorted(set(keys) | set(config.public_env) | {"SECRET_KEY"})
        if any(not _IDENTIFIER.fullmatch(key) for key in wanted) or not config.env_file:
            raise fail("env key 또는 env_file 설정 오류", ErrorCode.CONFIG_INVALID)
        changed = _private_env(Path(config.env_file), wanted, config.public_env)
        host.check_deadline()
        return ProviderResult(
            provider=self.name,
            function="inject_config",
            changed=changed,
            config_ref=config.env_file,
            keys=wanted,
        )

    def ensure_tls(self, mode: Literal["check", "apply"], ctx: RunContext) -> ProviderResult:
        del mode, ctx
        return ProviderResult(
            provider=self.name,
            function="ensure_tls",
            applicable=False,
            detail="해당 없음: 로컬 HTTPS는 보류(⏸)",
        )
