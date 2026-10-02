#!/usr/bin/env python3
"""개발 명령 단일 진입점. Makefile, CI, 문서는 모두 이 파일(=make 타깃)만 가리킨다.

- `setup`은 uv 설치 전에도 돌아야 하므로 시스템 python3로 실행한다: python3 scripts/dev.py setup
- 나머지는 uv 환경에서 실행한다: uv run python scripts/dev.py <작업>  (= make <작업>)
- 실행하는 명령을 먼저 출력하고, 실패하면 0이 아닌 코드로 끝난다. shell=True는 쓰지 않는다.
- 담당자가 아직 구현하지 않은 작업은 "미구현(담당 X)"을 출력하고 종료 코드 3으로 끝난다.

표준 라이브러리만 쓴다. setup 경로는 오래된 시스템 python3(3.9+)에서도 돌아야 하므로
3.10+ 전용 문법을 런타임에 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_IMPLEMENTED = 3
UV_MIN = (0, 12, 20)
UV_MAX_EXCLUSIVE = (0, 13, 0)


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------
def tool(name: str) -> str:
    """현재 파이썬(uv venv)과 같은 bin 디렉토리의 도구를 먼저 쓴다."""
    bindir = Path(sys.executable).parent
    for candidate in (bindir / name, bindir / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or name


def run(cmd: Sequence[str], *, env: dict[str, str] | None = None) -> int:
    print("$ " + " ".join(cmd), flush=True)
    merged = None if env is None else {**os.environ, **env}
    try:
        return subprocess.run(list(cmd), cwd=ROOT, env=merged, check=False).returncode
    except FileNotFoundError:
        print(f"dev: 명령을 찾을 수 없다: {cmd[0]}", file=sys.stderr)
        return 127


def run_all(cmds: Sequence[Sequence[str]]) -> int:
    for cmd in cmds:
        code = run(cmd)
        if code != 0:
            return code
    return 0


def capture(cmd: Sequence[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout.strip()


def not_implemented(owner: str, what: str) -> int:
    print(f"미구현(담당 {owner}): {what}")
    return NOT_IMPLEMENTED


def py(*args: str) -> list[str]:
    return [sys.executable, *args]


# ---------------------------------------------------------------------------
# 품질
# ---------------------------------------------------------------------------
def task_fmt() -> int:
    return run_all(
        [[tool("ruff"), "format", ".", "../src"], [tool("ruff"), "check", "--fix", ".", "../src"]]
    )


def task_lint() -> int:
    return run_all(
        [[tool("ruff"), "format", "--check", ".", "../src"], [tool("ruff"), "check", ".", "../src"]]
    )


def task_type(all_packages: bool = False) -> int:
    code = run([tool("pyright"), "--project", ".."])  # 루트 pyproject: src/ddak/core
    if all_packages:
        extra = run([tool("pyright"), "--project", "..", "../src"])
        if extra != 0:
            print("dev: 경고: src 전체 pyright 경고가 있다(차단하지 않음)")
    return code


def task_test(extra: Sequence[str] = ()) -> int:
    return run(py("-m", "pytest", *extra))


def task_test_marker(marker: str) -> int:
    return run(py("-m", "pytest", "-m", marker))


def task_contracts(update: bool = False) -> int:
    args = [] if update else ["--check"]
    return run(py("scripts/export_schemas.py", *args))


def task_boundary() -> int:
    return run_all(
        [
            [tool("lint-imports"), "--config", "../pyproject.toml"],
            py(
                "-m",
                "pytest",
                "tests/contract/test_registry.py",
                "tests/contract/test_registry_rules.py",
                "tests/contract/test_ai_guard.py",
                "tests/contract/test_ai_providers.py",
            ),
        ]
    )


def task_attribution(full: bool = False) -> int:
    code, _ = capture(["git", "rev-parse", "--verify", "--quiet", "origin/main"])
    # --full(제출 전 확인)이거나 원격 main이 없으면 전체 이력을 검사한다.
    base = "origin/main" if code == 0 and not full else ""
    return run(py("scripts/git_guard.py", "range", base, "HEAD"))


def _steps(with_attribution: bool) -> list[tuple[str, Callable[[], int]]]:
    steps: list[tuple[str, Callable[[], int]]] = [
        ("lint", task_lint),
        ("type", task_type),
        ("boundary", task_boundary),
        ("contracts", task_contracts),
        ("test", task_test),
    ]
    if with_attribution:
        steps.append(("attribution", task_attribution))
    return steps


def task_check(with_attribution: bool = True) -> int:
    results: list[tuple[str, int, float]] = []
    for name, fn in _steps(with_attribution):
        print(f"\n== {name} ==", flush=True)
        started = time.monotonic()
        code = fn()
        results.append((name, code, time.monotonic() - started))
    total = sum(r[2] for r in results)
    print("\n== 요약 ==")
    for name, code, secs in results:
        print(f"  {name:<12} {'PASS' if code == 0 else 'FAIL':<5} {secs:6.1f}s")
    print(f"  합계 {total:.1f}s (목표 90초 이내)")
    return 0 if all(code == 0 for _, code, _ in results) else 1


# ---------------------------------------------------------------------------
# 실행·데모
# ---------------------------------------------------------------------------
def task_run(mode: str) -> int:
    # 앱 1개(관리 웹 + 실행기 + 레지스트리)를 127.0.0.1에 띄운다. 호스트 실행(✅ 장부 11).
    print("dev: 앱을 띄운다(관리 웹 골격 + 레지스트리 + 실행 서비스 연결, UI/C3 별도).")
    return run(py("-m", "ddak"), env={"DDAK_ADAPTER_MODE": mode})


def task_demo_reset(cloud: bool, containers: bool = False) -> int:
    if cloud:
        return not_implemented("C2", "클라우드 reset은 지원하지 않으며 리소스를 변경하지 않는다")
    return run(py("scripts/o1_demo.py", "--reset", *(["--containers"] if containers else [])))


def task_secrets_scan() -> int:
    if shutil.which("gitleaks") is None:
        print("dev: gitleaks가 없다. 설치 후 다시 실행한다(CI secrets 잡은 범위만 스캔).")
        return 1
    return run(["gitleaks", "git", "--redact", "--no-banner", "."])


def task_tf_plan() -> int:
    tf_dir = ROOT / "infra" / "terraform"
    if not any(tf_dir.glob("*.tf")):
        return not_implemented("C1", "infra/terraform (*.tf 없음)")
    chdir = f"-chdir={tf_dir.relative_to(ROOT)}"
    # apply 타깃은 만들지 않는다. apply/destroy는 사람이 직접 실행한다.
    return run_all(
        [
            ["terraform", chdir, "fmt", "-check", "-recursive"],
            ["terraform", chdir, "init", "-input=false"],
            ["terraform", chdir, "validate"],
            ["terraform", chdir, "plan", "-input=false"],
        ]
    )


def task_clean() -> int:
    removed = 0
    targets = [ROOT / ".pytest_cache", ROOT / ".ruff_cache"]
    skip = {".venv", ".git", "node_modules", "var"}
    for path in ROOT.rglob("__pycache__"):
        if not skip.intersection(path.relative_to(ROOT).parts):
            targets.append(path)
    for path in targets:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            removed += 1
    print(f"dev: 캐시 {removed}개 디렉토리를 지웠다. var/ 런타임 상태·DB·잠금은 보존했다.")
    return 0


# ---------------------------------------------------------------------------
# setup (E-5): 저장소 로컬 git 설정만 바꾼다. 전역 설정은 건드리지 않는다.
# ---------------------------------------------------------------------------
def _uv_version() -> tuple[int, int, int] | None:
    code, out = capture(["uv", "--version"])
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out) if code == 0 else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _claude_attribution_ok() -> str | None:
    """사용자 ~/.claude/settings.json의 attribution 확인. 문제가 있으면 안내 문자열."""
    path = Path.home() / ".claude" / "settings.json"
    want = '{"attribution": {"commit": "", "pr": "", "sessionUrl": false}}'
    if not path.is_file():
        return f"~/.claude/settings.json이 없다. Claude Code를 쓰면 {want}를 넣는다."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return "~/.claude/settings.json을 JSON으로 읽지 못했다."
    attribution = data.get("attribution") if isinstance(data, dict) else None
    if isinstance(attribution, dict) and (
        attribution.get("commit") == ""
        and attribution.get("pr") == ""
        and attribution.get("sessionUrl") is False
    ):
        return None
    return f"~/.claude/settings.json attribution을 객체 형식 세 키로 설정한다: {want}"


def task_setup_local(no_sync: bool = False) -> int:
    """Git·사용자 설정에 접근하지 않고 로컬 파이썬 의존성만 준비한다."""
    version = _uv_version()
    if version is None or not (UV_MIN <= version < UV_MAX_EXCLUSIVE):
        print("dev: uv >=0.12.20,<0.13이 필요하다. 설치한 방식으로 업데이트한다.", file=sys.stderr)
        return 1
    if no_sync:
        return 0
    locked = ["--locked"] if any((p / "uv.lock").is_file() for p in (ROOT, ROOT.parent)) else []
    return run(["uv", "sync", *locked])


def task_setup(no_sync: bool) -> int:
    problems: list[str] = []
    warnings: list[str] = []
    code, git_root = capture(["git", "rev-parse", "--show-toplevel"])
    repository = Path(git_root.strip()).resolve()
    project = ROOT.resolve()
    if code != 0 or project not in (repository, repository / "harness"):
        print(
            "dev: 저장소 루트 또는 harness/에서 실행한다. 템플릿은 make setup-local을 쓴다.",
            file=sys.stderr,
        )
        return 1

    version = _uv_version()
    if version is None:
        problems.append("uv가 없다. https://docs.astral.sh/uv/ 에서 설치한다.")
    elif not (UV_MIN <= version < UV_MAX_EXCLUSIVE):
        shown = ".".join(map(str, version))
        problems.append(f"uv {shown}는 범위 밖(>=0.12.20,<0.13). 설치한 방식으로 업데이트한다.")
    elif not no_sync:
        # uv.lock이 있으면 --locked로 lock을 건드리지 않는다(첫 생성은 하네스 소유자만).
        locked = ["--locked"] if any((p / "uv.lock").is_file() for p in (ROOT, ROOT.parent)) else []
        if run(["uv", "sync", *locked]) != 0:
            problems.append("uv sync 실패. 출력 확인 후 다시 실행한다.")

    hooks_path = (project / ".githooks").relative_to(repository).as_posix()
    for key, value in (("core.hooksPath", hooks_path), ("user.useConfigOnly", "true")):
        if run(["git", "config", "--local", key, value]) != 0:
            problems.append(f"git config --local {key} 설정 실패")
    for hook in (ROOT / ".githooks").iterdir():
        if hook.is_file():
            hook.chmod(0o755)

    name = capture(["git", "config", "--get", "user.name"])[1]
    email = capture(["git", "config", "--get", "user.email"])[1].lower()
    if not name or not email:
        problems.append(
            "git user.name/user.email이 없다. GitHub noreply 주소로 직접 설정한다"
            "(예: git config user.email <id>+<login>@users.noreply.github.com)."
        )

    claude = _claude_attribution_ok()
    if claude:
        warnings.append(claude)

    print("\n== setup 결과 ==")
    for line in warnings:
        print(f"  경고: {line}")
    for line in problems:
        print(f"  실패: {line}")
    if problems:
        return 1
    print("  통과: 훅 설치, 신원 확인, 의존성 동기화 완료. 다음: make check")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev", description="개발 명령 단일 진입점")
    sub = parser.add_subparsers(dest="task", required=True, metavar="<작업>")
    p = sub.add_parser("setup", help="클론 직후 1회: uv, 의존성, 훅, git 신원 확인")
    p.add_argument("--no-sync", action="store_true", help="uv sync를 건너뛴다")
    p = sub.add_parser("setup-local", help="Git 없이 로컬 의존성만 준비")
    p.add_argument("--no-sync", action="store_true", help="uv 버전만 확인한다")
    sub.add_parser("fmt", help="ruff format + ruff check --fix")
    sub.add_parser("lint", help="ruff format --check + ruff check")
    p = sub.add_parser("type", help="pyright(core 차단, --all이면 나머지는 경고)")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("test", help="pytest(docker/aws/llm 마커 제외)")
    p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    for marker in ("docker", "aws", "llm"):
        sub.add_parser(f"test-{marker}", help=f"{marker} 마커 테스트만(담당자 수동)")
    sub.add_parser("contracts", help="스키마·툴 카탈로그 스냅샷 드리프트 검사")
    sub.add_parser("contracts-update", help="스냅샷 갱신(계약 변경 PR에서만)")
    sub.add_parser("boundary", help="import-linter + AI 경계 테스트")
    p = sub.add_parser("attribution", help="origin/main..HEAD 작성자·트레일러 검사")
    p.add_argument("--full", action="store_true", help="전체 이력 검사(제출 전)")
    sub.add_parser("check", help="push·PR 전 필수: lint→type→boundary→contracts→test→attribution")
    sub.add_parser("ci", help="check에서 attribution만 뺀 것(CI quality 잡)")
    sub.add_parser("run", help="앱 1개(관리 웹 + 실행기 + 레지스트리), real 어댑터")
    sub.add_parser("run-fake", help="같은 기동, 모든 어댑터 Fake")
    for name in ("onprem-run", "onprem-plan", "unlock"):
        p = sub.add_parser(name, help="온프렘 운영 진입점")
        p.add_argument("--project")
        p.add_argument("--ref")
    sub.add_parser("contract-smoke", help="레지스트리 구현 현황(등록/미구현) + Fake 1회 호출")
    p = sub.add_parser("demo-reset", help="O1 fixture 전용 상태 초기화(클라우드 미지원)")
    p.add_argument("--cloud", action="store_true")
    p.add_argument("--containers", action="store_true", help="이중 소유 라벨의 데모 컨테이너 정리")
    p = sub.add_parser("demo", help="명시적 fixture 시나리오 리허설(실제 클라우드 아님)")
    p.add_argument("--mode", choices=("fixture", "local", "real"), default="fixture")
    p.add_argument(
        "--scenario",
        choices=("success", "local_fail", "cloud_fail", "parity_fail"),
        default="success",
    )
    p.add_argument("--yes", action="store_true", help="fixture 테스트만 자동 승인")
    for name, help_text in (
        ("demo-local", "실제 Docker provider 통합 테스트(전체 서비스 데모 아님)"),
        ("smoke-local", "로컬 스모크 시나리오"),
        ("smoke-cloud", "클라우드 스모크 시나리오"),
        ("patch-eval", "AI 설정 패치 P0 평가(일반 기본 OFF, 데모 ON)"),
        ("preflight", "Docker 제한 시간 조회 + 팀 도구 등록/누락 현황(자격증명 미조회)"),
        ("secrets-scan", "gitleaks 전체 이력 스캔"),
        ("tf-plan", "terraform fmt/validate/plan (apply 없음)"),
        ("clean", "캐시만 삭제(var/ 런타임 상태·DB·잠금 보존)"),
    ):
        sub.add_parser(name, help=help_text)
    return parser


def dispatch(args: argparse.Namespace) -> int:
    task = args.task
    if task in {"onprem-run", "onprem-plan", "unlock"}:
        action = {"onprem-run": "run", "onprem-plan": "plan", "unlock": "unlock"}[task]
        extra = (["--project", args.project] if args.project else []) + (
            ["--ref", args.ref] if args.ref else []
        )
        return run(py("scripts/onprem_fullchain.py", action, *extra))
    simple: dict[str, Callable[[], int]] = {
        "fmt": task_fmt,
        "lint": task_lint,
        "contracts": task_contracts,
        "contracts-update": lambda: task_contracts(update=True),
        "boundary": task_boundary,
        "check": task_check,
        "ci": lambda: task_check(with_attribution=False),
        "run": lambda: task_run("real"),
        "run-fake": lambda: task_run("fake"),
        "contract-smoke": lambda: run(py("scripts/contract_smoke.py")),
        "patch-eval": lambda: run(py("scripts/patch_eval.py")),
        "demo-local": lambda: run(py("scripts/o1_demo.py", "--mode", "local")),
        "smoke-local": lambda: not_implemented("O3", "smoke-local"),
        "smoke-cloud": lambda: not_implemented("C3", "smoke-cloud"),
        "preflight": lambda: run(py("scripts/o1_demo.py", "--preflight")),
        "secrets-scan": task_secrets_scan,
        "tf-plan": task_tf_plan,
        "clean": task_clean,
    }
    if task == "setup":
        return task_setup(args.no_sync)
    if task == "setup-local":
        return task_setup_local(args.no_sync)
    if task == "type":
        return task_type(args.all)
    if task == "test":
        return task_test(args.pytest_args)
    if task.startswith("test-"):
        return task_test_marker(task.removeprefix("test-"))
    if task == "attribution":
        return task_attribution(args.full)
    if task == "demo-reset":
        return task_demo_reset(args.cloud, args.containers)
    if task == "demo":
        return run(
            py(
                "scripts/o1_demo.py",
                "--mode",
                args.mode,
                "--scenario",
                args.scenario,
                *(["--yes"] if args.yes else []),
            )
        )
    return simple[task]()


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
