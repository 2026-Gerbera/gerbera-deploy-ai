"""승인 해시에 묶인 제품 도구 준비. plan/probe는 명령을 설치하지 않는다."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import platform
import re
import stat
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import yaml

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.private_values import (
    private_directory,
    read_private,
    validated_path,
    write_private,
)

GITLEAKS_VERSION = "8.30.1"
_RELEASE = "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
# 공급망 근거(2026-10-03): 공식 API asset.digest와 checksum 원문을 서로 대조했다.
# https://api.github.com/repos/gitleaks/gitleaks/releases/tags/v8.30.1
# https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
# 에이전트는 릴리스 바이너리를 다운로드/설치하지 않았다. apply에서만 검증·추출한다.
_GITLEAKS_DIGESTS = {
    "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    "darwin_x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
}
_MAX_ARCHIVE = 32 * 1024 * 1024


class SetupRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input: str | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


class SetupDownloader(Protocol):
    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> bytes: ...


def _error(detail: str = "제품 초기 설정 실패; 원문 출력은 숨김") -> DdakToolError:
    return DdakToolError(ErrorCode.PRECONDITION_FAILED, detail)


def _runner(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=dict(env), input=input, timeout=timeout, capture_output=True, text=True
    )


def _release_url(url: str, *, asset: bool = False) -> bool:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or parts.fragment
        ):
            return False
        if asset and parts.hostname in {
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }:
            return True
        return url in {
            f"{_RELEASE}gitleaks_{GITLEAKS_VERSION}_{target}.tar.gz" for target in _GITLEAKS_DIGESTS
        }
    except ValueError:
        return False


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _release_url(newurl, asset=True):
            raise _error("공식 GitHub 릴리스 외 다운로드 경로는 허용하지 않는다")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, *, timeout: float, max_bytes: int) -> bytes:
    if not _release_url(url):
        raise _error()
    opener = urllib.request.build_opener(_ReleaseRedirect())
    with opener.open(url, timeout=timeout) as response:
        if not _release_url(response.geturl(), asset=True):
            raise _error()
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise _error()
    return data


def _call(
    runner: SetupRunner,
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str | None = None,
    timeout: float = 20,
) -> str:
    try:
        result = runner(argv, cwd=cwd, env=env, input=input, timeout=timeout)
        if result.returncode != 0 or len(result.stdout or "") > 65536:
            raise _error()
        return result.stdout or ""
    except Exception:
        raise _error() from None


def _binary(data: bytes, name: str) -> bytes:
    """추출 API로 경로를 만들지 않고 승인된 최상위 regular file 한 개만 읽는다."""
    try:
        payload = None
        seen: set[str] = set()
        allowed = {"gitleaks", "LICENSE", "README.md"}
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    mode = member.external_attr >> 16
                    if (
                        member.filename not in allowed
                        or member.filename in seen
                        or member.is_dir()
                        or stat.S_IFMT(mode) not in {0, 0o100000}
                        or member.file_size > _MAX_ARCHIVE
                        or member.flag_bits & 1
                    ):
                        raise ValueError
                    seen.add(member.filename)
                    if member.filename == "gitleaks":
                        payload = archive.read(member)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive:
                    if (
                        member.name not in allowed
                        or member.name in seen
                        or not member.isfile()
                        or member.size > _MAX_ARCHIVE
                    ):
                        raise ValueError
                    seen.add(member.name)
                    if member.name == "gitleaks":
                        source = archive.extractfile(member)
                        if source is None:
                            raise ValueError
                        with source:
                            payload = source.read(_MAX_ARCHIVE + 1)
        if not payload or len(payload) > _MAX_ARCHIVE:
            raise ValueError
        return payload
    except Exception:
        raise _error("도구 압축 파일의 경로·형식 또는 단일 바이너리 검증 실패") from None


def _pins() -> tuple[str, str]:
    # core -> cloud import 없이 설치된 ddak 패키지의 buildspec 리소스를 읽는다.
    try:
        raw = files("ddak").joinpath("cloud/build/buildspec.yml").read_bytes()
        spec = yaml.safe_load(raw)
        commands = spec["phases"]["pre_build"]["commands"]
        buildkit = [
            re.fullmatch(
                r"docker buildx create --name ddak --driver docker-container --driver-opt "
                r"image=(moby/buildkit@sha256:[0-9a-f]{64}) --use",
                command,
            )
            for command in commands
        ]
        builders = [match.group(1) for match in buildkit if match]
        if spec.get("version") != 0.2 or len(builders) != 1:
            raise ValueError
        return builders[0], hashlib.sha256(raw).hexdigest()
    except Exception:
        raise _error("패키지 buildspec의 고정 이미지 계약 검증 실패") from None


class DockerConfig:
    """사용자 기본 Docker 설정을 읽지 않는 제품 전용 config 폴더."""

    def __init__(self, path: str | Path, *, runner: SetupRunner | None = None) -> None:
        self.path = validated_path(path)
        self._runner = runner or _runner

    def __repr__(self) -> str:
        return "<DockerConfig>"

    def status(self) -> dict:
        try:
            with private_directory(self.path) as fd:
                data = json.loads(read_private(fd, "config.json"))
                auth = data.get("auths", {}).get("https://index.docker.io/v1/", {}).get("auth")
                configured = isinstance(auth, str) and bool(auth)
                if data.get("credsStore") or data.get("credHelpers"):
                    raise _error("제품 Docker 설정에 외부 자격증명 helper를 허용하지 않는다")
        except FileNotFoundError:
            configured = False
        except (OSError, ValueError, TypeError, AttributeError):
            raise _error("제품 Docker 설정 메타데이터 확인 실패") from None
        return {"configured": configured, "verified": False}

    def _initialize(self) -> None:
        with private_directory(self.path, create=True) as fd:
            try:
                self.status()
                data = json.loads(read_private(fd, "config.json"))
            except FileNotFoundError:
                data = {"auths": {}}
            # Docker ContainsAuth()가 false이면 OS credential helper를 자동 탐지한다.
            # 빈 registry 항목은 자격증명이 아니며 이 자동 탐지만 막는다.
            if not data.get("auths"):
                data["auths"] = {"https://index.docker.io/v1/": {}}
                write_private(fd, "config.json", json.dumps(data).encode())

    def login(self, username: str, token: str) -> dict:
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", username)
            or not isinstance(token, str)
            or not token
            or len(token) > 16384
            or any(c in token for c in "\r\n\x00")
        ):
            raise _error("Docker 로그인 입력 형식 오류")
        try:
            self._initialize()
            with private_directory(self.path) as fd:
                self.status()  # helper·링크·권한을 CLI 실행 전에 거부한다.
                _call(
                    self._runner,
                    [
                        "docker",
                        "--config",
                        str(self.path),
                        "login",
                        "--username",
                        username,
                        "--password-stdin",
                        "docker.io",
                    ],
                    cwd=self.path,
                    env={
                        "PATH": os.environ.get("PATH", os.defpath),
                        "DOCKER_CONFIG": str(self.path),
                    },
                    input=token + "\n",
                    timeout=30,
                )
                # CLI가 만든 파일도 링크를 따라가지 않고 0600으로 제한한다.
                stream = os.open(
                    "config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
                try:
                    meta = os.fstat(stream)
                    if (
                        not stat.S_ISREG(meta.st_mode)
                        or meta.st_uid != os.getuid()
                        or meta.st_nlink != 1
                        or meta.st_size > 65536
                    ):
                        raise _error()
                    os.fchmod(stream, 0o600)
                finally:
                    os.close(stream)
                status = self.status()
                data = json.loads(read_private(fd, "config.json"))
                auth = data.get("auths", {}).get("https://index.docker.io/v1/", {}).get("auth", "")
                try:
                    confirmed = status["configured"] and hmac.compare_digest(
                        base64.b64decode(auth, validate=True), (username + ":" + token).encode()
                    )
                except (ValueError, TypeError):
                    confirmed = False
                return {
                    **status,
                    "status": "green" if confirmed else "gray",
                    "login_confirmed": bool(confirmed),
                    "detail": "제품 Docker 로그인 확인됨; 저장소 push 권한은 별도 확인 필요"
                    if confirmed
                    else "제품 Docker 로그인 저장 결과 미확인",
                }
        except Exception:
            raise _error("Docker 로그인 실패; 자격증명과 원문 출력은 숨김") from None


class BuildSetup:
    """root는 main이 정한 프로젝트별 setup 폴더. 승인 토큰은 메모리에만 있다."""

    def __init__(
        self,
        root: str | Path,
        *,
        runner: SetupRunner | None = None,
        downloader: SetupDownloader | None = None,
        builder_name: str | None = None,
    ) -> None:
        if builder_name is not None and (
            not isinstance(builder_name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", builder_name)
        ):
            raise _error("기존 빌더 이름 형식 오류")
        self.root = validated_path(root)
        self._runner = runner or _runner
        self._downloader = downloader or _download
        self._builder_name = builder_name
        self._approved: str | None = None

    @property
    def docker_config(self) -> Path:
        return self.root / "docker"

    @property
    def tool_dir(self) -> Path:
        return self.root / "tools" / "gitleaks" / GITLEAKS_VERSION

    @property
    def builder_name(self) -> str:
        return self._builder_name or (
            "ddak-product-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        )

    def _env(self) -> dict[str, str]:
        # local.py와 동일한 연결 환경. 출력/계획에는 환경변수 값을 싣지 않는다.
        allowed = (
            "PATH",
            "HOME",
            "DOCKER_CONFIG",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CERT_PATH",
        )
        return {
            **{key: os.environ[key] for key in allowed if key in os.environ},
            "PATH": str(self.tool_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
            "DOCKER_CONFIG": str(self.docker_config),
            "BUILDX_BUILDER": self.builder_name,
        }

    def plan(self) -> dict:
        validated_path(self.root)
        system = platform.system().lower()
        machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
            platform.machine().lower(), "unsupported"
        )
        target = system + "_" + machine
        supported = target in _GITLEAKS_DIGESTS
        buildkit, spec_hash = _pins()
        archive = f"gitleaks_{GITLEAKS_VERSION}_{target}.tar.gz"
        plan = {
            "versions": {"gitleaks": GITLEAKS_VERSION, "uv": ">=0.12.20,<0.13"},
            "target": target,
            "supported": supported,
            "digests": {
                "archive": _GITLEAKS_DIGESTS.get(target),
                "buildkit": buildkit,
                "buildspec": spec_hash,
            },
            "downloads": {"archive": _RELEASE + archive} if supported else {},
            "paths": {
                "docker_config": str(self.docker_config),
                "builder": self.builder_name,
                "tool_dir": str(self.tool_dir),
            },
            "actions": [
                {
                    "type": "install_gitleaks",
                    "version": GITLEAKS_VERSION,
                    "path": str(self.tool_dir / "gitleaks"),
                    "verify_sha256": _GITLEAKS_DIGESTS.get(target),
                },
            ]
            if supported
            else [],
        }
        if supported and self._builder_name is None:
            plan["actions"].extend(
                [
                    {
                        "type": "command",
                        "argv": [
                            "docker",
                            "buildx",
                            "create",
                            "--name",
                            self.builder_name,
                            "--driver",
                            "docker-container",
                            "--driver-opt",
                            "image=" + buildkit,
                        ],
                    },
                    {
                        "type": "command",
                        "argv": ["docker", "buildx", "inspect", self.builder_name, "--bootstrap"],
                    },
                ]
            )
        plan["hash"] = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return plan

    def approve(self, plan_hash: str) -> None:
        if not isinstance(plan_hash, str) or plan_hash != self.plan()["hash"]:
            self._approved = None
            raise _error("초기 설정 승인 해시가 현재 계획과 다르다")
        self._approved = plan_hash

    def login(self, username: str, token: str) -> dict:
        return DockerConfig(self.docker_config, runner=self._runner).login(username, token)

    def _installation(self) -> dict | None:
        """공개 가능한 정형 메타데이터만 반환한다. 원문/추가 필드는 반환하지 않는다."""
        try:
            with private_directory(self.root) as fd:
                state = json.loads(read_private(fd, "setup-state.json"))
            patterns = {
                "version": r"\d{1,3}\.\d{1,3}\.\d{1,3}",
                "archive_sha256": r"[0-9a-f]{64}",
                "binary_sha256": r"[0-9a-f]{64}",
                "installed_at": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00",
            }
            if not isinstance(state, dict) or any(
                not isinstance(state.get(key), str) or not re.fullmatch(pattern, state[key])
                for key, pattern in patterns.items()
            ):
                return None
            datetime.fromisoformat(state["installed_at"])
            return {key: state[key] for key in patterns}
        except (OSError, ValueError, DdakToolError):
            return None

    def probe(self) -> dict:
        labels = {
            "gitleaks": "Gitleaks",
            "uv": "uv",
            "docker": "Docker",
            "buildx": "Buildx",
            "builder": "빌더",
            "platforms": "빌드 플랫폼",
        }
        measured = dict.fromkeys(labels, False)
        checks = {
            key: {
                "id": key,
                "label": label,
                "status": "red",
                "detail": "검사 미완료",
                "version": None,
            }
            for key, label in labels.items()
        }

        def mark(key, ok, detail, version=None):
            measured[key] = bool(ok)
            checks[key].update(status="green" if ok else "red", detail=detail, version=version)

        warnings = ["Docker 자격증명 유효성·저장소 push 권한은 확인되지 않았다"]
        installation = self._installation()
        try:
            plan = self.plan()
        except Exception:
            detail = "도구 준비 계획 검사 실패; 제품 경로·BuildKit 고정 이미지 계약 확인 필요"
            for ident in checks:
                mark(ident, False, detail)
            return {
                "status": "blocked",
                "ready": False,
                "measured": measured,
                "checks": list(checks.values()),
                "installation": installation,
                "detail": detail,
                "warnings": warnings,
                "paths": {
                    "docker_config": str(self.docker_config),
                    "builder": self.builder_name,
                    "tool_dir": str(self.tool_dir),
                },
                "credentials_verified": False,
            }
        cwd = self.root if self.root.is_dir() else Path.cwd()
        env = self._env()
        mark("gitleaks", False, "제품 Gitleaks 미설치 또는 설치 기록 불완전")
        try:
            if not plan["supported"]:
                mark("gitleaks", False, "지원하지 않는 플랫폼: Linux/macOS amd64·arm64만 지원")
            elif installation is not None:
                with private_directory(self.tool_dir) as fd:
                    binary = read_private(fd, "gitleaks", mode=0o700, limit=_MAX_ARCHIVE)
                if (
                    installation["version"] != GITLEAKS_VERSION
                    or installation["archive_sha256"] != plan["digests"]["archive"]
                    or installation["binary_sha256"] != hashlib.sha256(binary).hexdigest()
                ):
                    mark("gitleaks", False, "Gitleaks 설치 버전 또는 무결성 불일치")
                else:
                    output = _call(
                        self._runner, [str(self.tool_dir / "gitleaks"), "version"], cwd=cwd, env=env
                    ).strip()
                    version = (
                        output.removeprefix("v")
                        if re.fullmatch(r"v?\d{1,3}\.\d{1,3}\.\d{1,3}", output)
                        else None
                    )
                    ok = version == GITLEAKS_VERSION
                    mark(
                        "gitleaks",
                        ok,
                        "Gitleaks 8.30.1 무결성·버전 확인됨" if ok else "Gitleaks 실행 버전 불일치",
                        version,
                    )
        except FileNotFoundError:
            mark("gitleaks", False, "Gitleaks 설치 불완전: 제품 바이너리 없음")
        except Exception:
            mark("gitleaks", False, "Gitleaks 설치 불완전: 권한·무결성 또는 실행 검사 실패")

        try:
            output = _call(self._runner, ["uv", "--version"], cwd=cwd, env=env).strip()
            match = re.fullmatch(r"uv (\d{1,3}\.\d{1,3}\.\d{1,3})(?: \([^\r\n]{1,160}\))?", output)
            version = match[1] if match else None
            ok = version is not None and (0, 12, 20) <= tuple(map(int, version.split("."))) < (
                0,
                13,
                0,
            )
            mark(
                "uv",
                ok,
                "uv 요구 버전 확인됨" if ok else "uv 버전 불일치: >=0.12.20,<0.13 필요",
                version,
            )
        except Exception:
            mark("uv", False, "uv 없음 또는 버전 검사 실패; 자동 설치하지 않음")

        # 기본 Docker 자격증명 helper를 건드리지 않도록 제품 config 확인 후 조회한다.
        mark("docker", False, "제품 Docker 설정 없음 또는 Docker 연결 검사 실패")
        mark("buildx", False, "Docker 연결 확인 전; Buildx 검사 미완료")
        mark("builder", False, "Docker 연결 확인 전; 빌더 검사 미완료")
        mark("platforms", False, "빌더 확인 전; 플랫폼 검사 미완료")
        try:
            config = DockerConfig(self.docker_config, runner=self._runner)
            config.status()
            with private_directory(self.docker_config) as fd:
                if not json.loads(read_private(fd, "config.json")).get("auths"):
                    raise _error()
            _call(self._runner, ["docker", "system", "info"], cwd=cwd, env=env)
            mark("docker", True, "Docker 연결 확인됨; 저장소 권한은 미확인")
        except Exception:
            mark("docker", False, "제품 Docker 설정 없음 또는 Docker 연결 검사 실패")
        if measured["docker"]:
            try:
                output = _call(self._runner, ["docker", "buildx", "version"], cwd=cwd, env=env)
                buildx_version = re.search(r"\bv?(\d{1,3}\.\d{1,3}\.\d{1,3})\b", output)
                mark(
                    "buildx",
                    buildx_version is not None,
                    "Buildx 버전 확인됨" if buildx_version else "Buildx 버전 확인 실패",
                    buildx_version[1] if buildx_version else None,
                )
            except Exception:
                mark("buildx", False, "Buildx 없음 또는 버전 검사 실패")
        if measured["buildx"]:
            self._probe_builder(plan, cwd, env, mark)
        ready = all(measured.values())
        return {
            "status": "ready" if ready else "blocked",
            "ready": ready,
            "measured": measured,
            "checks": list(checks.values()),
            "installation": installation,
            "detail": "제품 빌드 도구 검사 완료" if ready else "제품 빌드 도구 준비 미확인",
            "warnings": warnings,
            "paths": plan["paths"],
            "credentials_verified": False,
        }

    def _probe_builder(self, plan, cwd, env, mark) -> None:
        try:
            output = _call(
                self._runner, ["docker", "buildx", "inspect", self.builder_name], cwd=cwd, env=env
            )
            name = re.search(r"^Name:\s*(\S+)\s*$", output, re.MULTILINE)
            driver = re.search(
                r"^Driver:\s*(docker|docker-container|kubernetes|remote)\s*$", output, re.MULTILINE
            )
            pin = plan["digests"]["buildkit"]
            images = re.findall(r"\bimage=[\"']?([^\"'\s]+)", output)
            image = bool(images) and all(value == pin for value in images)
            valid = bool(
                name
                and name[1] == self.builder_name
                and driver
                and (self._builder_name is not None or (driver[1] == "docker-container" and image))
            )
            mark(
                "builder",
                valid,
                "빌더 이름·드라이버 확인됨"
                if valid
                else "빌더 이름·드라이버 또는 고정 이미지 불일치",
            )
            platforms: set[str] = set()
            running = False
            for line in output.splitlines():
                if line.strip().startswith("Name:"):
                    running = False
                if re.fullmatch(r"Status:\s+running", line.strip()):
                    running = True
                if running and line.strip().startswith("Platforms:"):
                    platforms.update(
                        p.strip() for p in line.split(":", 1)[1].split(",") if "*" not in p
                    )
            supported = (
                valid
                and "linux/amd64" in platforms
                and any(p == "linux/arm64" or p.startswith("linux/arm64/v") for p in platforms)
            )
            mark(
                "platforms",
                supported,
                "실행 중인 빌더의 amd64·arm64 지원 확인됨"
                if supported
                else "실행 중인 빌더의 amd64·arm64 지원 미확인; QEMU/binfmt는 자동 등록하지 않음",
            )
        except Exception:
            mark("builder", False, "빌더 미등록 또는 조회 실패")

    def apply(self, plan_hash: str) -> dict:
        plan = self.plan()
        if self._approved != plan_hash or plan_hash != plan["hash"]:
            self._approved = None
            raise _error("현재 계획 해시의 별도 초기 설정 승인이 필요하다")
        self._approved = None  # 실패·재시도에도 새 승인 필요.
        if not plan["supported"]:
            return self.probe()
        try:
            for folder in (self.root, self.tool_dir, self.docker_config):
                with private_directory(folder, create=True):
                    pass
            DockerConfig(self.docker_config, runner=self._runner)._initialize()
            current = self.probe()
            if current["ready"]:
                return current
            if not current["measured"]["gitleaks"]:
                archive = self._downloader(
                    plan["downloads"]["archive"], timeout=60, max_bytes=_MAX_ARCHIVE
                )
                if (
                    not isinstance(archive, bytes)
                    or len(archive) > _MAX_ARCHIVE
                    or hashlib.sha256(archive).hexdigest() != plan["digests"]["archive"]
                ):
                    raise _error("Gitleaks 공식 릴리스 SHA-256 검증 실패")
                name = plan["downloads"]["archive"].rsplit("/", 1)[-1]
                binary = _binary(archive, name)
                with private_directory(self.tool_dir) as fd:
                    write_private(fd, "gitleaks", binary, mode=0o700)
                with private_directory(self.root) as fd:
                    write_private(
                        fd,
                        "setup-state.json",
                        json.dumps(
                            {
                                "version": GITLEAKS_VERSION,
                                "archive_sha256": plan["digests"]["archive"],
                                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                                "installed_at": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        ).encode(),
                    )
                # 설치 명령 실패는 예외로, 버전/플랫폼 불일치 등 부분 준비는 blocked로 반환한다.
                _call(
                    self._runner,
                    [str(self.tool_dir / "gitleaks"), "version"],
                    cwd=self.root,
                    env=self._env(),
                )
            if not current["measured"]["docker"] or not current["measured"]["buildx"]:
                return self.probe()
            deadline = time.monotonic() + 180
            for action in plan["actions"][1:]:
                argv = action["argv"]
                if current["measured"]["builder"] and "create" in argv:
                    continue
                if current["measured"]["platforms"] and "--bootstrap" in argv:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error("제품 초기 설정 제한 시간 초과")
                _call(
                    self._runner, argv, cwd=self.root, env=self._env(), timeout=min(60, remaining)
                )
            result = self.probe()
            return result
        except DdakToolError:
            raise
        except Exception:
            raise _error("제품 초기 설정 실패; 원문 출력과 자격증명은 숨김") from None
