"""동일 tier replica의 순차 교체·준비 확인·멱등 복구."""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import ErrorCode
from ddak.core.contracts.release import ImageObservation
from ddak.onprem.deploy.config import env_key_names
from ddak.onprem.deploy.containers import DockerHost, fail, image_ref

if TYPE_CHECKING:
    from ddak.onprem.deploy.provider import OnPremProvider, _Tier

# argv로 받는 포트/경로/시간 이외에는 인벤토리나 앱 코드를 실행하지 않는다.
_READY_SCRIPT = """import http.client, sys, time
port, path, wait = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
end = time.monotonic() + wait
while time.monotonic() < end:
    timeout = min(1, max(.01, end-time.monotonic()))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        if conn.getresponse().status == 200:
            sys.exit(0)
    except (OSError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    time.sleep(min(.5, max(0, end-time.monotonic())))
print("DDAK_READY_FAILURE")
sys.exit(1)
"""


class ReplicaUnhealthy(Exception):
    """관측된 앱 상태 실패. Docker/SSH 전송 실패와 구분한다."""


def health_args(config: _Tier) -> list[str]:
    if config.ready is None:
        return []
    command = shlex.join(
        ["python", "-c", _READY_SCRIPT, str(config.ready.port), config.ready.path, "1"]
    )
    return [
        "--health-cmd",
        command,
        "--health-interval",
        "1s",
        "--health-timeout",
        "2s",
        "--health-retries",
        "1",
        "--health-start-period",
        "0s",
    ]


def names(config: _Tier) -> list[tuple[int, str]]:
    if config.replicas is None:
        return [(1, config.name)]
    return [(i, f"{config.name}-{i}") for i in range(1, config.replicas + 1)]


def check_ready(
    host: DockerHost,
    config: _Tier,
    state: dict,
    local: dict,
    observation: ImageObservation,
    ref: str,
    project: str,
    tier: str,
) -> None:
    host.owned(state, project, tier)
    if (
        not state["State"]["Running"]
        or state.get("RestartCount", 0) != 0
        or state["Config"]["Image"] != ref
        or not host.matches(state, local, observation)
    ):
        raise ReplicaUnhealthy("배포 이미지 실행 관측 실패")
    if config.ready is None:
        return
    wait = min(config.ready.timeout_s, host.deadline - time.monotonic() - 30)
    if wait <= 0:
        raise ReplicaUnhealthy("replica 준비 확인 시간 부족")
    end = time.monotonic() + wait
    result = host.run(
        "container",
        "exec",
        state["Id"],
        "python",
        "-c",
        _READY_SCRIPT,
        str(config.ready.port),
        config.ready.path,
        str(wait),
        check=False,
        timeout=wait + 10,
    )
    if result.returncode:
        if result.returncode == 1 and result.stdout.strip() == "DDAK_READY_FAILURE":
            raise ReplicaUnhealthy("replica 준비 확인 실패")
        raise fail("Docker 준비 확인 명령 실패")
    while True:
        checked = host.inspect_container(state["Id"])
        host.owned(checked, project, tier)
        if (
            not checked["State"]["Running"]
            or checked.get("RestartCount", 0) != 0
            or not host.matches(checked, local, observation)
        ):
            raise ReplicaUnhealthy("replica 준비 확인 뒤 실행 관측 실패")
        health = checked["State"].get("Health") or {}
        if not health.get("Status"):
            raise ReplicaUnhealthy("Docker healthcheck 없음")
        if health["Status"] == "healthy":
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise ReplicaUnhealthy("Docker healthcheck 준비 확인 실패")
        time.sleep(min(0.2, remaining))


def replace_replicas(
    provider: OnPremProvider,
    host: DockerHost,
    config: _Tier,
    tier: str,
    ctx: RunContext,
    ref: str,
    function: str,
    *,
    strict_env: bool = False,
) -> ProviderResult:
    ref = image_ref(ref)
    base_args = provider._args(config, ctx.project, tier)
    before = {}
    for i, name in names(config):
        current = host.container(name)
        if current:
            host.owned(current, ctx.project, tier)
        staged = host.container(name + "-ddak-next")
        if staged:
            host.owned(staged, ctx.project, tier)
        before[i] = current
    # 이미지 검증 전에 어떤 replica도 멈추지 않는다. tier당 한 번만 조회한다.
    host.run("image", "pull", "--platform", config.platform, ref)
    observation, local_image = host.image(ref, config.platform)
    if (
        function == "deploy"
        and ctx.release_artifacts is not None
        and tier in ctx.release_artifacts.images
    ):
        expected = ctx.release_artifacts.images[tier].platform_digests[observation.platform]
        if observation.platform_digest != expected:
            raise fail("빌드 산출물과 실제 플랫폼 manifest 불일치")
    provider._volumes(host, config, local_image)
    release_id = ctx.run_id
    if function == "rollback":
        release_id = ctx.previous_release.get("local", {}).get("release_id")
    if not isinstance(release_id, str) or not release_id or any(c in release_id for c in "\n\r\0"):
        raise fail("RELEASE_ID 형식 오류", ErrorCode.CONFIG_INVALID)
    spec = {
        "config": config.model_dump(),
        "release_id": release_id,
    }
    env_hash = hashlib.sha256(
        json.dumps(env_key_names(Path(config.env_file)) if config.env_file else []).encode()
    ).hexdigest()
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    updated = []
    previous = next((s["Config"]["Image"] for s in before.values() if s), None)
    for i, name in names(config):
        current = before[i]
        staged_name = name + "-ddak-next"
        staged = host.container(staged_name)
        changed = False
        if staged:
            host.remove(staged, ctx.project, tier)
            changed = True
        same = (
            current is not None
            and current["Config"]["Image"] == ref
            and host.matches(current, local_image, observation)
            and current["Config"]["Labels"].get("ddak.spec") == spec_hash
            and (not strict_env or current["Config"]["Labels"].get("ddak.envkeys") == env_hash)
        )
        if same and current is not None:
            if not current["State"]["Running"]:
                host.run("container", "start", current["Id"])
                changed = True
        else:
            args = [
                *base_args,
                "--label",
                f"ddak.spec={spec_hash}",
                "--label",
                f"ddak.envkeys={env_hash}",
                "--label",
                f"ddak.platform={observation.platform}",
                "--label",
                f"ddak.platform-digest={observation.platform_digest}",
                "--label",
                f"ddak.config-digest={local_image['ConfigDigest']}",
                "-e",
                f"RELEASE_ID={release_id}",
                *health_args(config),
            ]
            if config.replicas is not None:
                args += ["--label", f"ddak.replica={i}"]
            if config.traefik_labels:
                labels = {**config.traefik_labels, "traefik.docker.network": config.network}
                for key, value in labels.items():
                    args += ["--label", f"{key}={value}"]
            for port in config.ports:
                args += ["--publish", port]
            host.run("container", "create", "--name", staged_name, *args, ref)
            staged = host.container(staged_name)
            if not staged:
                raise fail("대체 컨테이너 생성 관측 실패")
            host.owned(staged, ctx.project, tier)
            if not host.matches(staged, local_image, observation):
                raise fail("대체 컨테이너 플랫폼 manifest 불일치")
            if current:
                host.remove(current, ctx.project, tier)
            host.run("container", "rename", staged["Id"], name)
            host.run("container", "start", staged["Id"])
            changed = True
        running = host.container(name)
        if not running:
            raise fail("배포 컨테이너 관측 실패")
        try:
            check_ready(host, config, running, local_image, observation, ref, ctx.project, tier)
        except ReplicaUnhealthy as error:
            # 확인된 불량 replica는 롤백을 기다리는 동안에도 트래픽을 받지 않는다.
            checked = host.inspect_container(running["Id"])
            host.owned(checked, ctx.project, tier)
            if checked["State"]["Running"]:
                host.run("container", "stop", "--time", "1", checked["Id"])
            if host.inspect_container(checked["Id"])["State"]["Running"]:
                raise fail("준비 실패 replica 정지 미확인", ErrorCode.ADAPTER_TIMEOUT) from None
            raise fail(str(error)) from None
        if changed:
            updated.append(i)
    return ProviderResult(
        provider=provider.name,
        function=function,
        changed=bool(updated),
        previous_image=previous,
        image_ref=ref,
        observation=observation,
        detail=f"{'복구' if function == 'rollback' else '교체'} replica: {updated}",
    )


def remove_replicas(
    provider: OnPremProvider, host: DockerHost, config: _Tier, tier: str, ctx: RunContext
) -> ProviderResult:
    found = []
    for i, name in names(config):
        for candidate in (name, name + "-ddak-next"):
            current = host.container(candidate)
            if current:
                host.owned(current, ctx.project, tier)
                found.append((i, current))
    for _, current in found:
        host.remove(current, ctx.project, tier)
    return ProviderResult(
        provider=provider.name,
        function="rollback",
        changed=bool(found),
        previous_image=found[0][1]["Config"]["Image"] if found else None,
        detail=f"제거 replica: {sorted({i for i, _ in found})}",
    )
