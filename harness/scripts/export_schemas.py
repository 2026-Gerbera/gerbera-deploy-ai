#!/usr/bin/env python3
"""계약 스키마 스냅샷: contracts/schemas/ 를 다시 만들거나(--check면) 비교만 한다.

생성물(손으로 고치지 않는다):
- <tool>.input.json / <tool>.output.json : ddak.core.contracts.tools.<tool>의 <Tool>Input/Output
- plan.json, events.json, deploy_config.json : 해당 모델이 정의돼 있을 때만
- tool_catalog.json : 레지스트리 카탈로그(40개 + 예시 ping)의 메타정보. MCP tools/list 스냅샷 대신

JSON Schema는 pydantic model_json_schema(mode="validation"), 정렬·들여쓰기 2칸·끝 줄바꿈으로
고정해 결정적으로 만든다. 사용: make contracts (검사) / make contracts-update (갱신).
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "contracts" / "schemas"
TOP_LEVEL = (
    ("plan.json", "ddak.core.contracts.plan", "Plan"),
    ("events.json", "ddak.core.contracts.events", "RunEvent"),
    ("deploy_config.json", "ddak.core.contracts.deploy_config", "DeployConfig"),
    ("approval.json", "ddak.core.contracts.approval", "ApprovalRecord"),
    ("release_artifacts.json", "ddak.core.contracts.release", "ReleaseArtifacts"),
)

if str(ROOT / "src") not in sys.path:  # 설치 전(스크래치·CI 첫 실행)에도 import되게
    sys.path.insert(0, str(ROOT / "src"))


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def model_schemas() -> Iterator[tuple[str, str]]:
    from ddak.core.contracts import tools as tools_pkg
    from ddak.core.contracts.base import ContractModel

    for info in sorted(pkgutil.iter_modules(tools_pkg.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{tools_pkg.__name__}.{info.name}")
        for suffix in ("Input", "Output"):
            model = getattr(module, f"{_pascal(info.name)}{suffix}", None)
            if isinstance(model, type) and issubclass(model, ContractModel):
                schema = model.model_json_schema(mode="validation")
                yield f"{info.name}.{suffix.lower()}.json", _dump(schema)
    for filename, module_name, attr in TOP_LEVEL:
        model = getattr(importlib.import_module(module_name), attr, None)
        if isinstance(model, type) and issubclass(model, ContractModel):
            yield filename, _dump(model.model_json_schema(mode="validation"))


def catalog_snapshot() -> tuple[str, str]:
    from ddak.core.registry import CATALOG

    rows = [spec.model_dump(mode="json") for spec in sorted(CATALOG, key=lambda s: s.name)]
    return "tool_catalog.json", _dump(rows)


def expected_files() -> dict[str, str]:
    files = dict(model_schemas())
    name, content = catalog_snapshot()
    files[name] = content
    return files


def check() -> list[str]:
    """드리프트 목록(비어 있으면 일치). 파일을 쓰지 않는다."""
    expected = expected_files()
    problems: list[str] = []
    for name, content in sorted(expected.items()):
        path = OUT_DIR / name
        if not path.is_file():
            problems.append(f"없음: contracts/schemas/{name}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"다름: contracts/schemas/{name}")
    if OUT_DIR.is_dir():
        for path in sorted(OUT_DIR.glob("*.json")):
            if path.name not in expected:
                problems.append(f"남는 파일: contracts/schemas/{path.name}")
    return problems


def write() -> list[str]:
    expected = expected_files()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUT_DIR.glob("*.json"):
        if path.name not in expected:
            path.unlink()
    for name, content in expected.items():
        (OUT_DIR / name).write_text(content, encoding="utf-8")
    return sorted(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="쓰지 않고 비교만 한다")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for line in problems:
            print(f"export_schemas: {line}", file=sys.stderr)
        if problems:
            print("계약 스냅샷 드리프트. 계약 변경 PR이면 `make contracts-update` 후 커밋한다.")
            return 1
        print("export_schemas: 스냅샷 일치")
        return 0
    written = write()
    print(f"export_schemas: {len(written)}개 파일을 contracts/schemas/에 썼다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
