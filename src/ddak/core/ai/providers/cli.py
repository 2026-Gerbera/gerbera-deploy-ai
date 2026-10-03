"""cli backend(개발): 운영자 본인 로컬의 Claude CLI를 `claude -p`로 부른다.

확인 필요: 플래그 조합과 결과 JSON 키(structured_output, usage, total_cost_usd, duration_ms,
is_error)는 CLI 2.1.284 실측 기준이다(research/2026-09-30_웹-CLI-LLM호출-검토.md). 설치한 버전에서
`make test-llm`으로 다시 확인한다. 아래 규칙은 바꾸지 않는다.

- 쉘 없이 인자 배열로 실행한다. 프롬프트는 stdin으로만 넘긴다(argv에 넣지 않음 -> ps에 안 보임).
- 빈 임시 cwd에서 실행한다(--safe-mode와 함께: cwd의 프로젝트 훅·설정이 실행되지 않게).
- env 허용목록: PATH, HOME, USER, LANG. USER가 빠지면 macOS 키체인 조회가 실패해 "Not logged in".
  ANTHROPIC_API_KEY·AWS 자격증명은 넘기지 않는다(-p는 키가 있으면 구독 대신 키를 쓴다).
- 타임아웃이면 프로세스 그룹 전체를 종료한다.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from typing import Any, Literal

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.contracts.base import AIUsage
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact

# 도구 전부 끔 + 권한 요청 자동 거부 + 세션 저장 안 함(확인 필요: CLI 2.1.284 실측 조합)
HARDENED_FLAGS: tuple[str, ...] = (
    "-p",
    "--safe-mode",
    "--restricted",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools",
    "",
    "--disallowedTools",
    "mcp__*",
    "--permission-mode",
    "dontAsk",
    "--permission-prompts",
    "none",
    "--no-session-persistence",
    "--max-turns",
    "2",
    "--max-budget-usd",
    "0.5",
)
# 절대 넣지 않는 플래그. --bare는 OAuth(구독)를 읽지 않으므로 cli backend에서 쓰지 않는다.
FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "bypassPermissions",
        "--allowedTools",
        "--bare",
    }
)
ENV_ALLOW: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG")


def build_argv(
    claude_bin: str,
    *,
    system: str,
    json_schema: Mapping[str, Any],
    model: str | None,
    effort: Literal["low", "medium"] = "low",
) -> list[str]:
    if effort not in ("low", "medium"):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "CLI effort는 low/medium만 허용한다")
    argv = [claude_bin, *HARDENED_FLAGS]
    if model:
        argv += ["--model", model]
    argv += [
        "--effort",
        effort,
        "--system-prompt",
        system,
        "--json-schema",
        json.dumps(dict(json_schema), sort_keys=True, ensure_ascii=False),
        "--output-format",
        "json",
    ]
    if FORBIDDEN_FLAGS.intersection(argv):
        raise DdakToolError(ErrorCode.INTERNAL, "금지 플래그가 CLI 인자에 들어갔다")
    return argv


def build_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """허용목록 밖의 키는 절대 넘기지 않는다."""
    env = os.environ if environ is None else environ
    return {key: env[key] for key in ENV_ALLOW if key in env}


def parse_result(stdout: str, *, model: str | None) -> AIResponse:
    """`--output-format json` 결과 한 덩어리를 AIResponse로 바꾼다(확인 필요: 키 이름)."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "CLI 출력이 JSON이 아니다") from exc
    if not isinstance(data, dict):
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "CLI 출력 최상위가 객체가 아니다")
    if data.get("is_error"):
        # 안전장치 오탐(예: 시크릿·IAM 문맥)도 여기로 온다. 호출한 툴이 규칙 대체 경로로 간다.
        raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "CLI가 오류를 돌려줬다(is_error)")
    structured = data.get("structured_output")
    text = json.dumps(structured, ensure_ascii=False) if structured is not None else ""
    if not text:
        text = str(data.get("result") or "")
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    ai_usage = AIUsage(
        model=model or "claude-cli-default",
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(data.get("total_cost_usd") or 0.0),  # 구독이면 청구 대신 한도 차감
        latency_ms=int(data.get("duration_ms") or 0),
    )
    return AIResponse(text=text, source=Source.LIVE, usage=ai_usage)


class ClaudeCliProvider:
    name = "cli"

    def __init__(
        self, claude_bin: str = "claude", *, effort: Literal["low", "medium"] = "low"
    ) -> None:
        self._bin = claude_bin
        self._effort: Literal["low", "medium"] = effort

    def complete(self, req: AIRequest) -> AIResponse:
        argv = build_argv(
            self._bin,
            system=req.system,
            json_schema=req.json_schema,
            model=req.model,
            effort=self._effort,
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="llm-") as cwd:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=build_env(),
                    text=True,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "claude 실행 파일이 없다") from exc
            try:
                stdout, stderr = proc.communicate(req.user, timeout=req.timeout_s)
            except subprocess.TimeoutExpired as exc:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate()
                raise DdakToolError(ErrorCode.AI_UNAVAILABLE, "CLI 타임아웃") from exc
        if proc.returncode != 0:
            detail = redact(stderr or "", max_len=300)
            raise DdakToolError(
                ErrorCode.AI_UNAVAILABLE, f"CLI 종료 코드 {proc.returncode}: {detail}"
            )
        response = parse_result(stdout, model=req.model)
        if response.usage is not None and response.usage.latency_ms == 0:
            elapsed = int((time.monotonic() - started) * 1000)
            response = AIResponse(
                text=response.text,
                source=response.source,
                usage=response.usage.model_copy(update={"latency_ms": elapsed}),
            )
        return response


def cli_status(claude_bin: str = "claude", timeout_s: float = 10.0) -> dict[str, Any]:
    """설치/인증만 확인한다. 연결 green은 명시적 fixed probe가 맡는다.

    확인된 명령 auth status --json을 사용한다. version/원문/이메일을 반환하지 않는다.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="llm-status-") as cwd:
            proc = subprocess.run(
                [claude_bin, "auth", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=build_env(),
                cwd=cwd,
                check=False,
            )
        data = json.loads(proc.stdout)
        logged_in = isinstance(data, dict) and data.get("loggedIn") is True
        if proc.returncode != 0 or not logged_in:
            return {
                "backend": "cli",
                "ok": False,
                "status": "red",
                "detail": "Claude CLI 미로그인 또는 인증 오류",
            }
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {
            "backend": "cli",
            "ok": False,
            "status": "red",
            "detail": "Claude CLI 설치/인증 확인 실패",
        }
    return {
        "backend": "cli",
        "ok": False,
        "status": "gray",
        "detail": "Claude CLI 인증됨; 연결 테스트 전",
    }
