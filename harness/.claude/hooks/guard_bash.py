#!/usr/bin/env python3
"""Claude Code PreToolUse(Bash) 가드 (💭 고려안, TL이 실제 차단을 1회 확인한다).

stdin으로 받은 훅 입력 JSON의 tool_input.command를 검사한다. 사람만 해야 하는 git/인프라
명령이면 사유를 stderr에 쓰고 종료 코드 2로 막는다(Claude Code는 2를 차단으로 처리하고
사유를 모델에게 보여 준다). 입력을 해석하지 못하면 경고만 쓰고 0으로 통과시킨다.

한계: Claude Code에만 적용된다. Codex나 사람의 터미널은 막지 못한다. 최종 관문은 CI다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import re
import sys

# 명령을 ; && || | 줄바꿈 단위로 자른다.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
# 따옴표 안 문자열(커밋 메시지 등)은 오탐을 줄이려고 비운다.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# bash -c "..." / eval "..." 처럼 따옴표 안이 명령인 경우는 안쪽도 검사한다.
_WRAPPER = re.compile(r"\b((ba|z)?sh\s+-c|eval)\b")

_RAW_RULES: tuple[tuple[str, str], ...] = (
    (r"--no-verify\b", "--no-verify로 git 훅을 우회하지 않는다"),
    (r"\bgit\b.*\bcommit\b.*\s-[a-z]*n[a-z]*(\s|$)", "git commit -n(훅 우회)을 쓰지 않는다"),
    (
        r"\bgit\b.*\bpush\b.*"
        r"(\s--force(-with-lease)?\b|\s-[a-z]*f[a-z]*(\s|$)|\s\+\S|\s--mirror\b)",
        "force push는 사람만 한다",
    ),
    (
        r"\bgit\b.*\bconfig\b.*(\buser\.(name|email)\s+\S|--(unset|unset-all)\s+user\.)",
        "git 신원(user.*) 설정을 바꾸지 않는다",
    ),
    (r"\bgit\b.*\s-c\s*user\.(name|email)\s*=", "git -c user.*로 신원을 바꾸지 않는다"),
    (r"core\.hookspath", "core.hooksPath를 바꾸지 않는다(훅 무력화)"),
    (r"\bGIT_(AUTHOR|COMMITTER)_(NAME|EMAIL|DATE)\s*=", "GIT_AUTHOR_* 등 신원 변수를 쓰지 않는다"),
    (r"\bgit\b.*\bfilter-(branch|repo)\b", "이력 재작성(git filter-*)은 TL만 한다"),
    (r"\bgh\s+pr\s+merge\b", "merge는 사람만 한다"),
    (r"\bterraform\b.*\s(apply|destroy)\b", "terraform apply/destroy는 사람만 한다"),
    (r"\baws\b.*[\s-](delete|terminate|deregister)-[a-z0-9-]+", "AWS 리소스 삭제는 사람만 한다"),
    (r"\baws\s+s3\s+(rm|rb)\b", "S3 객체·버킷 삭제는 사람만 한다"),
    # Read deny(.claude/settings.json)는 cat·grep 같은 셸 명령을 막지 못한다. 비밀 파일 경로가
    # 명령에 나오면 막는다. env.example·.env.example은 허용한다.
    (
        r"(^|[\s=<>/])\.env(\.(?!example\b)[\w.-]+)?(\s|$)"
        r"|\.tfstate\b|\.aws/(credentials|config)\b|\.ssh/id_|\.mcp-inspector\b"
        r"|(^|[\s=<>/])\.secrets/|\.claude/\.credentials\.json",
        "비밀 파일(.env, .secrets/, tfstate, ~/.aws, ~/.ssh 키, Claude 자격증명)을 셸로 읽지 않음",
    ),
)
RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _RAW_RULES
)


def segments(command: str) -> list[str]:
    """검사할 명령 조각들. 따옴표 안은 비우고, 래퍼(bash -c, eval)의 안쪽은 따로 꺼낸다."""
    found: list[str] = []
    for seg in _SEGMENT_SPLIT.split(command):
        seg = seg.strip()
        if not seg:
            continue
        found.append(_QUOTED.sub("''", seg))
        if _WRAPPER.search(seg):
            for quoted in _QUOTED.findall(seg):
                found.extend(segments(quoted[1:-1]))
    return found


def violations(command: str) -> list[str]:
    """차단 사유 목록. 비어 있으면 허용."""
    reasons: list[str] = []
    for seg in segments(command):
        for pattern, reason in RULES:
            if pattern.search(seg) and reason not in reasons:
                reasons.append(reason)
    return reasons


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command", "")
        if not isinstance(command, str):
            raise TypeError("tool_input.command가 문자열이 아니다")
    except (ValueError, TypeError, AttributeError) as exc:
        sys.stderr.write(f"guard_bash: 경고: 훅 입력을 해석하지 못해 통과시킨다: {exc}\n")
        return 0
    reasons = violations(command)
    if not reasons:
        return 0
    sys.stderr.write("guard_bash: 저장소 규칙(AGENTS.md)상 사람만 실행하는 명령이다.\n")
    for reason in reasons:
        sys.stderr.write(f"  - {reason}\n")
    sys.stderr.write("필요하면 사용자에게 직접 실행을 요청한다.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
