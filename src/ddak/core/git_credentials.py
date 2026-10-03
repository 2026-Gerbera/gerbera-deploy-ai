"""제품 Git의 명령 한정 신원과 URL에 묶인 credential-helper. 토큰은 argv/env에 없다."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.private_values import SecretVault
from ddak.core.project_settings import ProjectSettings, watch_source


def configured_identity(saved: dict, repo_path: Path | None = None) -> tuple[str, str]:
    """명시한 사람 신원 우선. 앱 checkout/global 설정은 읽기만 한다."""
    values = {key: saved.get(key) for key in ("git_author_name", "git_author_email")}
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    for field, key in (("git_author_name", "user.name"), ("git_author_email", "user.email")):
        if values[field] is not None:
            continue
        commands = []
        if repo_path is not None and (repo_path / ".git").exists():
            commands.append(["git", "-C", str(repo_path), "config", "--local", "--get", key])
        commands.append(["git", "config", "--global", "--get", key])
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0 and result.stdout.strip():
                values[field] = result.stdout.strip()
                break
    try:
        cfg = ProjectSettings.model_validate(values)
        if cfg.git_author_name and cfg.git_author_email:
            return cfg.git_author_name, cfg.git_author_email
    except ValueError:
        pass
    raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "설정 필요: 앱 커밋 작성자 이름·이메일")


def save_token(root: Path, project: str, url: str, token: str) -> None:
    cfg = ProjectSettings(repo_url=url)
    if not cfg.repo_url or urlsplit(url).hostname != "github.com":
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "GitHub 앱 저장소 URL을 먼저 저장하세요")
    if not token or len(token) > 4096 or any(c.isspace() or ord(c) < 32 for c in token):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 토큰 형식 오류")
    SecretVault(root).put(project, "git_push_token", json.dumps({"url": url, "token": token}))


def require_token(root: Path, project: str, url: str) -> str:
    try:
        raw = SecretVault(root).get(project, "git_push_token")
        value = json.loads(raw or "{}")
        if watch_source(value["url"]) != watch_source(url):
            raise ValueError
        token = value["token"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError
        return token
    except (ValueError, KeyError, TypeError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "설정 필요: 이 앱 저장소의 push 토큰"
        ) from None


def helper_options(root: Path, project: str, url: str) -> list[str]:
    helper = "!" + shlex.join(
        [sys.executable, "-P", "-m", "ddak.core.git_credentials", str(root), project, url]
    )
    return [
        "credential.helper=",
        "credential.helper=" + helper,
        "credential.useHttpPath=true",
        "http.extraHeader=",
    ]


def isolated_git_env(environment: dict[str, str]) -> dict[str, str]:
    # 상속된 Git 설정/trace/대화형 인증은 제품 토큰보다 우선할 수 없다.
    env = {
        k: v
        for k, v in environment.items()
        if not k.startswith(("GIT_CONFIG", "GIT_TRACE")) and k != "GIT_CURL_VERBOSE"
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
    )
    return env


def respond(root: Path, project: str, url: str, operation: str, request: str) -> str:
    """Git 전용 stdin/stdout 프로토콜. store/erase는 받지 않고 호스트·경로를 제한한다."""
    if operation != "get" or len(request) > 8192:
        return ""
    fields = {}
    for line in request.splitlines():
        if not line:
            break
        if "=" not in line:
            return ""
        key, value = line.split("=", 1)
        if key in fields:
            return ""
        fields[key] = value
    parsed = urlsplit(url)
    if (
        fields.get("protocol") != "https"
        or parsed.hostname != "github.com"
        or fields.get("host") != parsed.netloc
        or fields.get("path", "").rstrip("/").removesuffix(".git")
        != parsed.path.lstrip("/").rstrip("/").removesuffix(".git")
    ):
        return ""
    token = require_token(root, project, url)
    return "username=x-access-token\npassword=" + token + "\n\n"


def main() -> None:
    # Git은 helper 출력을 credential 입력으로만 받는다. 오류/traceback은 출력하지 않는다.
    try:
        root, project, url, operation = sys.argv[1:]
        answer = respond(Path(root), project, url, operation, sys.stdin.read(8193))
        sys.stdout.write(answer)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
