"""결정 12의 단일 내용 판정: 이전 diff 복원과 값 줄의 재등장 탐지."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.patch_config import MAX_PATCH_CHARS, PatchViolation
from ddak.core.patch_patterns import PATTERNS
from ddak.plan.patch.check import _parse, _unsafe_path


@dataclass(frozen=True)
class PreviousFile:
    old: str
    new: str
    removed: tuple[str, ...]


def previous_files(patch: bytes) -> dict[str, PreviousFile]:
    """파일 전체 단일 hunk만 복원한다. 손상된 이력을 빈 이력으로 취급하지 않는다."""

    def invalid() -> DdakToolError:
        return DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 승인 패치 형식 불일치")

    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid() from None
    parsed, problems = _parse(text)
    if (
        not patch
        or len(patch) > MAX_PATCH_CHARS
        or problems
        or not parsed
        or len({f.path for f in parsed}) != len(parsed)
        or any(_unsafe_path(f.path) or f.created_or_deleted for f in parsed)
    ):
        raise invalid()
    lines = re.findall(r"[^\n]*\n|[^\n]+$", text)
    files: dict[str, PreviousFile] = {}
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if not lines[i].startswith("--- a/") or i + 2 >= len(lines):
            raise invalid()
        path = lines[i][6:].rstrip("\n")
        if lines[i + 1] != f"+++ b/{path}\n":
            raise invalid()
        match = re.match(r"^@@ -([01])(?:,(\d+))? \+([01])(?:,(\d+))? @@", lines[i + 2])
        if match is None:
            raise invalid()
        old_left = int(match.group(2) or "1")
        new_left = int(match.group(4) or "1")
        old: list[str] = []
        new: list[str] = []
        removed: list[str] = []
        last = ""
        i += 3
        while i < len(lines) and (old_left or new_left or lines[i].startswith("\\")):
            tag, body = lines[i][0], lines[i][1:]
            if tag == "\\":
                for side, applies in ((old, last in (" ", "-")), (new, last in (" ", "+"))):
                    if applies and side and side[-1].endswith("\n"):
                        side[-1] = side[-1][:-1]
            elif tag == " " and old_left and new_left:
                old.append(body)
                new.append(body)
                old_left -= 1
                new_left -= 1
            elif tag == "-" and old_left:
                old.append(body)
                removed.append(body.rstrip("\r\n"))
                old_left -= 1
            elif tag == "+" and new_left:
                new.append(body)
                new_left -= 1
            else:
                raise invalid()
            if tag != "\\":
                last = tag
            i += 1
        if old_left or new_left:
            raise invalid()
        files[path] = PreviousFile("".join(old), "".join(new), tuple(removed))
    return files


def lost_violations(
    previous: Mapping[str, PreviousFile], final: Mapping[str, str]
) -> tuple[PatchViolation, ...]:
    """줄 원문은 반환하지 않는다. 개발자가 값 줄이나 파일을 없앴으면 손실이 아니다."""
    violations = []
    for path, prev in sorted(previous.items()):
        values = {
            line.strip()
            for line in prev.removed
            if line.strip() and any(pattern.search(line) for pattern in PATTERNS.values())
        }
        for number, line in enumerate(final.get(path, "").splitlines(), 1):
            if line.strip() in values:
                violations.append(PatchViolation(code="patch_lost", file=path, line=number))
                if len(violations) >= 50:
                    return tuple(violations)
    return tuple(violations)
