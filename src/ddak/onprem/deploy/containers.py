"""로컬 Docker CLI 경계(옛 cd/docker_host.py).

argv만 사용하며 출력/예외에 env와 stderr를 노출하지 않는다.

Runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]를 주입한다.
각 호출은 남은 monotonic deadline과 command_timeout 중 작은 값으로 제한된다.
Docker API 1.49+ 및 buildx가 필요하다. manifest는 registry에서 읽고 SHA256을 검증한 뒤
선택한 child manifest의 config digest와 classic image ID를 대조한다. containerd는
native Descriptor/ImageManifestDescriptor를 대조한다. ID의 의미를 저장소별로 구분한다.
inspect는 Env를 포함하지 않는 필드만 요청한다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Protocol, cast

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.release import ImageObservation, Platform

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_CONTAINER_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},'
    '"ImageManifestDescriptor":{{json (index . "ImageManifestDescriptor")}},'
    '"Config":{"Image":{{json .Config.Image}},"Labels":{{json .Config.Labels}}},'
    '"RestartCount":{{json .RestartCount}},'
    '"Mounts":{{json .Mounts}},'
    '"State":{"Running":{{json .State.Running}},'
    '"Health":{{json (index .State "Health")}}}}'
)
_IMAGE_FORMAT = (
    '{"Id":{{json .Id}},"Os":{{json .Os}},"Architecture":{{json .Architecture}},'
    '"Descriptor":{{json (index . "Descriptor")}},'
    '"Volumes":{{json (index .Config "Volumes")}}}'
)


def fail(message: str, code: ErrorCode = ErrorCode.ADAPTER_FAILED) -> DdakToolError:
    return DdakToolError(code, message)


def image_ref(value: object) -> str:
    if not isinstance(value, str) or not IMAGE.fullmatch(value):
        raise fail("이미지는 sha256 digest로 고정해야 한다", ErrorCode.CONFIG_INVALID)
    return value


class Runner(Protocol):
    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def subprocess_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    # buildx는 자식 프로세스다. 부모 CLI만 kill하면 자식이 pipe를 붙잡아 deadline을
    # 넘길 수 있으므로 같은 프로세스 그룹을 정리한다. daemon RPC 취소 보장은 별개다.
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class DockerHost:
    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        deadline: float | None = None,
        command_timeout: float = 60,
        endpoint: str | None = None,
        local_registry: bool = False,
    ) -> None:
        self.runner = runner
        self.deadline = deadline if deadline is not None else time.monotonic() + 180
        self.command_timeout = command_timeout
        self.endpoint = endpoint
        self.local_registry = local_registry
        self.mutation_started = False
        if not math.isfinite(self.deadline) or not math.isfinite(command_timeout):
            raise fail("deadline이 유효하지 않다", ErrorCode.CONFIG_INVALID)
        if command_timeout <= 0:
            raise fail("command timeout은 양수여야 한다", ErrorCode.CONFIG_INVALID)
        if endpoint is not None and not (
            endpoint.startswith("unix:///")
            or re.fullmatch(r"ssh://[a-zA-Z0-9][a-zA-Z0-9_.-]*", endpoint)
        ):
            raise fail(
                "Docker endpoint는 unix 소켓 또는 ssh 별칭이어야 한다", ErrorCode.CONFIG_INVALID
            )

    def check_deadline(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise fail("Docker 작업 deadline 초과", ErrorCode.ADAPTER_TIMEOUT)
        return min(remaining, self.command_timeout)

    def _invoke(
        self, argv: list[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        timeout = (
            min(self.deadline - time.monotonic(), timeout) if timeout else self.check_deadline()
        )
        if timeout <= 0:
            raise fail("Docker 작업 deadline 초과", ErrorCode.ADAPTER_TIMEOUT)
        try:
            result = self.runner(argv, timeout=timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            raise fail("Docker 명령 시간 초과", ErrorCode.ADAPTER_TIMEOUT) from None
        except (OSError, subprocess.SubprocessError):
            raise fail("Docker CLI 실행 실패") from None
        self.check_deadline()
        return result

    def run(
        self, *args: str, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if self.endpoint is None:
            # 파일을 읽지 않고 endpoint만 조회한다. 원격 context에 로컬 env를 보내지 않는다.
            endpoint = os.environ.get("DOCKER_HOST")
            if not endpoint:
                result = self._invoke(
                    ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
                )
                if result.returncode:
                    raise fail("Docker context 조회 실패")
                endpoint = result.stdout.strip()
            if not endpoint.startswith("unix:///"):
                raise fail("로컬 Docker context가 필요하다", ErrorCode.CONFIG_INVALID)
            self.endpoint = endpoint
        if args[:2] in (
            ("container", "create"),
            ("container", "stop"),
            ("container", "rm"),
            ("container", "rename"),
            ("container", "start"),
        ):
            self.mutation_started = True
        result = self._invoke(["docker", "--host", self.endpoint, *args], timeout)
        if check and result.returncode:
            raise fail("Docker 명령 실패")
        return result

    def json(self, *args: str) -> Any:
        try:
            return json.loads(self.run(*args).stdout)
        except (ValueError, TypeError):
            raise fail("Docker JSON 응답이 유효하지 않다") from None

    def container(self, name: str) -> dict[str, Any] | None:
        # daemon 장애/권한 오류를 '없음'으로 오인하지 않는다.
        ids = self.run(
            "container", "ls", "-aq", "--filter", f"name=^/{re.escape(name)}$"
        ).stdout.split()
        if not ids:
            return None
        if len(ids) != 1:
            raise fail("컨테이너 이름이 유일하지 않다")
        return self.inspect_container(ids[0])

    def inspect_container(self, identifier: str) -> dict[str, Any]:
        value = self.json("container", "inspect", "--format", _CONTAINER_FORMAT, identifier)
        if not isinstance(value, dict):
            raise fail("컨테이너 관측 형식 오류")
        return value

    @staticmethod
    def owned(state: Mapping[str, Any], project: str, tier: str) -> None:
        labels = state.get("Config", {}).get("Labels") or {}
        expected = {"ddak.managed": "true", "ddak.project": project, "ddak.tier": tier}
        if any(labels.get(key) != value for key, value in expected.items()):
            raise fail("소유 라벨이 다른 컨테이너는 변경할 수 없다", ErrorCode.PRECONDITION_FAILED)

    def remove(
        self, state: Mapping[str, Any], project: str, tier: str, *, stop_seconds: int = 10
    ) -> None:
        # 이름 대신 immutable ID로 다시 관측하여 이름 바꿔치기를 막는다.
        current = self.json("container", "inspect", "--format", _CONTAINER_FORMAT, state["Id"])
        self.owned(current, project, tier)
        if current["State"]["Running"]:
            self.run("container", "stop", "--time", str(stop_seconds), current["Id"])
        current = self.json("container", "inspect", "--format", _CONTAINER_FORMAT, state["Id"])
        self.owned(current, project, tier)
        self.run("container", "rm", current["Id"])

    def _manifest(self, ref: str) -> dict[str, Any]:
        if self.local_registry:
            result = self._invoke(["docker", "buildx", "imagetools", "inspect", "--raw", ref])
            if result.returncode:
                raise fail("registry manifest 조회 실패")
            raw = result.stdout
        else:
            raw = self.run("buildx", "imagetools", "inspect", "--raw", ref).stdout
        expected = ref.rsplit("@", 1)[1]
        # buildx 버전에 따라 출력 끝에 LF 하나를 붙인다. JSON 재직렬화는 하지 않는다.
        if not any(
            "sha256:" + hashlib.sha256(candidate.encode()).hexdigest() == expected
            for candidate in (raw, raw.removesuffix("\n"))
        ):
            raise fail("registry manifest digest 불일치")
        try:
            value = json.loads(raw)
        except ValueError:
            raise fail("registry manifest JSON 오류") from None
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            raise fail("지원하지 않는 registry manifest")
        return value

    def image(self, ref: str, platform: str) -> tuple[ImageObservation, dict[str, Any]]:
        image_ref(ref)
        local = self.json(
            "image", "inspect", "--platform", platform, "--format", _IMAGE_FORMAT, ref
        )
        if not isinstance(local, dict):
            raise fail("image inspect 응답 오류")
        actual_platform = f"{local.get('Os')}/{local.get('Architecture')}"
        if actual_platform != platform:
            raise fail("실제 이미지 플랫폼 불일치")
        manifest = self._manifest(ref)
        digest = ref.rsplit("@", 1)[1]
        if "manifests" in manifest:
            matches = [
                child
                for child in manifest["manifests"]
                if f"{child.get('platform', {}).get('os')}/"
                f"{child.get('platform', {}).get('architecture')}" == platform
            ]
            if len(matches) != 1 or not DIGEST.fullmatch(matches[0].get("digest", "")):
                raise fail("플랫폼 child manifest를 유일하게 선택할 수 없다")
            digest = matches[0]["digest"]
            manifest = self._manifest(ref.rsplit("@", 1)[0] + "@" + digest)
        config_digest = manifest.get("config", {}).get("digest", "")
        if not DIGEST.fullmatch(config_digest):
            raise fail("manifest config digest 형식 오류")
        descriptor = local.get("Descriptor") or {}
        native = (
            descriptor.get("digest") == digest
            and descriptor.get("mediaType")
            in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }
            and local.get("Id") == digest
        )
        if not native and config_digest != local.get("Id"):
            raise fail("manifest와 로컬 이미지 관측 불일치")
        local["ConfigDigest"] = config_digest
        return ImageObservation(
            platform=cast(Platform, actual_platform), platform_digest=digest
        ), local

    @staticmethod
    def matches(
        state: Mapping[str, Any], local: Mapping[str, Any], observation: ImageObservation
    ) -> bool:
        descriptor = state.get("ImageManifestDescriptor") or {}
        if descriptor:
            return descriptor.get("digest") == observation.platform_digest and descriptor.get(
                "mediaType"
            ) in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }
        # classic store에서는 실행 컨테이너 Image가 config digest다. index ID는 인정하지 않는다.
        return state.get("Image") == local["ConfigDigest"]
