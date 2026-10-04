"""값을 제거한 파일에서만 표시용 변경 줄을 만든다."""

from __future__ import annotations

import ast
import difflib
import io
import re
import tokenize
from pathlib import Path

KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MASK = "[가림 · 문자열]"


def masked_code(code: str) -> str:
    """파일 전체를 파싱한다. 불완전한 코드는 줄 수를 유지하며 닫는다."""
    lines = code.splitlines(keepends=True)
    try:
        tree = ast.parse(code)
        allowed = set()
        kinds = {}

        def target_kind(node):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else str(node.slice.value)
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                else ""
            )
            if re.search(
                r"secret|password|passwd|token|credential|api_?key|private|_key$", name, re.I
            ):
                return "비밀값"
            if re.search(r"url|uri|host|address", name, re.I):
                return "주소"
            return None

        for assignment in ast.walk(tree):
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else (
                    [assignment.target]
                    if isinstance(assignment, ast.AnnAssign | ast.NamedExpr)
                    else []
                )
            )
            kind = next((target_kind(target) for target in targets if target_kind(target)), None)
            value = getattr(assignment, "value", None)
            if kind and isinstance(value, ast.AST):
                for literal in ast.walk(value):
                    if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                        column = len(
                            lines[literal.lineno - 1].encode()[: literal.col_offset].decode()
                        )
                        kinds[(literal.lineno, column)] = kind
        for node in ast.walk(tree):
            keys = []
            if isinstance(node, ast.Subscript):
                keys = [node.slice]
            elif isinstance(node, ast.Dict):
                keys = node.keys
            elif isinstance(node, ast.Call) and ast.unparse(node.func) in {
                "os.getenv",
                "getenv",
                "os.environ.get",
                "environ.get",
            }:
                keys = node.args[:1]
            for key in keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and KEY.fullmatch(key.value)
                ):
                    # AST 열은 UTF-8 바이트, tokenizer 열은 문자 기준이다.
                    column = len(lines[key.lineno - 1].encode()[: key.col_offset].decode())
                    allowed.add((key.lineno, column))
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        spans = []
        fstring_start = None
        fstring_depth = 0
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type == getattr(tokenize, "FSTRING_START", -1):
                if fstring_depth == 0:
                    fstring_start = token.start
                fstring_depth += 1
            if fstring_depth:
                if token.type == getattr(tokenize, "FSTRING_END", -1):
                    fstring_depth -= 1
                    if fstring_depth == 0 and fstring_start is not None:
                        # 중첩 표현식과 문자열 내부 값도 한 구간으로 가린다.
                        newline_count = token.end[0] - fstring_start[0]
                        quote = '"""' if newline_count else '"'
                        spans.append(
                            (
                                offsets[fstring_start[0] - 1] + fstring_start[1],
                                offsets[token.end[0] - 1] + token.end[1],
                                quote + MASK + "\n" * newline_count + quote,
                            )
                        )
                continue
            replacement = None
            if token.type == tokenize.STRING:
                mask = f"[가림 · {kinds.get(token.start, '문자열')}]"
                if token.end[0] != token.start[0]:
                    # 문자열 내부 줄을 부분 노출하지 않는다.
                    replacement = '"' + mask + '"' + "\n" * (token.end[0] - token.start[0])
                elif token.start not in allowed:
                    replacement = '"' + mask + '"'
            elif token.type == tokenize.NUMBER:
                replacement = '"[가림 · 숫자]"'
            elif token.type == tokenize.COMMENT:
                replacement = "# [가림 · 주석]"
            if replacement is not None:
                spans.append(
                    (
                        offsets[token.start[0] - 1] + token.start[1],
                        offsets[token.end[0] - 1] + token.end[1],
                        replacement,
                    )
                )
        for start, end, replacement in reversed(spans):
            code = code[:start] + replacement + code[end:]
        return code
    except (SyntaxError, ValueError, tokenize.TokenError, IndentationError):
        return "\n".join("[가림 · 코드 줄]" for _ in lines)


def _compact_rows(rows: list[dict], context: int = 3) -> list[dict]:
    """변경 앞뒤 문맥을 남기고 나머지는 생략 행 하나로 표시한다."""
    visible = set()
    for index, row in enumerate(rows):
        if row["kind"] in {"add", "del"}:
            visible.update(range(max(0, index - context), min(len(rows), index + context + 1)))
    compact = []
    omitted = 0
    for index, row in enumerate(rows):
        if index not in visible:
            omitted += 1
            continue
        if omitted:
            compact.append({"kind": "gap", "count": omitted, "old": None, "new": None, "text": ""})
            omitted = 0
        compact.append(row)
    if omitted:
        compact.append({"kind": "gap", "count": omitted, "old": None, "new": None, "text": ""})
    return compact


def code_changes(patch: str | None, source: Path | None = None) -> list[dict]:
    """원본으로 변경 위치를 구분하고, 가린 코드의 변경 앞뒤 3줄만 표시한다."""
    files = []
    current = None
    hunk = None
    for line in (patch or "").splitlines():
        if line.startswith("--- a/"):
            current = {"file": line[6:], "hunks": []}
            files.append(current)
        elif line.startswith("@@") and current is not None:
            found = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if found:
                hunk = {"old": int(found[1]), "new": int(found[2]), "before": [], "after": []}
                current["hunks"].append(hunk)
        elif hunk is not None and not line.startswith(("+++", "\\")) and line:
            if line[0] in " -":
                hunk["before"].append(line[1:])
            if line[0] in " +":
                hunk["after"].append(line[1:])
    cards = []
    for file in files:
        full_before = full_after = None
        if source is not None:
            path = source / file["file"]
            try:
                if path.resolve().is_relative_to(source.resolve()) and not path.is_symlink():
                    raw = path.read_text().splitlines()
                    changed = list(raw)
                    for item in reversed(file["hunks"]):
                        start = max(0, item["old"] - 1)
                        if raw[start : start + len(item["before"])] != item["before"]:
                            raise ValueError("표시용 원본 불일치")
                        changed[start : start + len(item["before"])] = item["after"]
                    full_before = masked_code("\n".join(raw)).splitlines()
                    full_after = masked_code("\n".join(changed)).splitlines()
            except (OSError, UnicodeError, ValueError):
                pass
        for item in file["hunks"]:
            before = (
                full_before[max(0, item["old"] - 1) : item["old"] - 1 + len(item["before"])]
                if full_before is not None
                else masked_code("\n".join(item["before"])).splitlines()
            )
            after = (
                full_after[max(0, item["new"] - 1) : item["new"] - 1 + len(item["after"])]
                if full_after is not None
                else masked_code("\n".join(item["after"])).splitlines()
            )
            rows = []
            for kind, a, b, c, d in difflib.SequenceMatcher(
                a=item["before"], b=item["after"], autojunk=False
            ).get_opcodes():
                if kind in {"equal", "delete", "replace"}:
                    rows.extend(
                        {
                            "kind": "ctx" if kind == "equal" else "del",
                            "old": item["old"] + n,
                            "new": item["new"] + c + n - a if kind == "equal" else None,
                            "text": before[n],
                        }
                        for n in range(a, b)
                    )
                if kind in {"insert", "replace"}:
                    rows.extend(
                        {"kind": "add", "old": None, "new": item["new"] + n, "text": after[n]}
                        for n in range(c, d)
                    )
            rows = _compact_rows(rows)
            visible = [row for row in rows if row["kind"] != "gap"]
            if not visible:
                continue
            positions = [row["old"] or row["new"] for row in visible]
            cards.append(
                {
                    "file": file["file"],
                    "line": min(positions),
                    "end": max(positions),
                    "before": "\n".join(before),
                    "after": "\n".join(after),
                    "rows": rows,
                }
            )
    return cards
