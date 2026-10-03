"""수정11: 필수/선택 읽기, 제품 공급 키, 기존 source_keys API."""

from pathlib import Path

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectInput
from ddak.core.runtime import tool_context
from ddak.plan.analyze import analyze_project, rules


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, mode: RunMode = RunMode.UPDATE, changed: tuple[str, ...] = ()):
    inp = AnalyzeProjectInput(
        run_id="fix11",
        request=DeployRequest(
            project="demo", repo_url="https://github.com/example/demo", target="both"
        ),
        source_dir="source",
        changed={},
        changed_paths=changed,
    )
    ctx = RunContext(
        "fix11",
        mode=mode,
        deploy_config={"tiers": {"was": {"paths": ["app"]}}, "env_example": ".env.example"},
    )
    with tool_context("analyze_project", "fix11"):
        return analyze_project(inp, ctx, root=root)


def test_optional_required_mixed_and_product_supplied(tmp_path: Path) -> None:
    _write(
        tmp_path / "source",
        ".env.example",
        "PORT=\nLOG_LEVEL=\nSECRET_KEY=\nRELEASE_ID=\nSOURCE_SHA=\nDB_HOST=\n",
    )
    _write(
        tmp_path / "source",
        "app/config.py",
        'os.getenv("PORT", "8080")\n'
        'os.environ.get("LOG_LEVEL")\n'
        'os.getenv("SECRET_KEY")\n'
        'os.environ["SECRET_KEY"]\n'
        'os.environ["RELEASE_ID"]\n'
        'os.getenv("SOURCE_SHA")\n',
    )
    out = _run(tmp_path, RunMode.BOOTSTRAP)
    keys = {key.name: key for key in out.env_keys}
    assert {name: key.required for name, key in keys.items()} == {
        "PORT": False,
        "LOG_LEVEL": False,
        "SECRET_KEY": True,
        "RELEASE_ID": False,
        "SOURCE_SHA": False,
        "DB_HOST": True,
    }
    assert all(key.is_new for key in keys.values())
    assert keys["RELEASE_ID"].kind == keys["SOURCE_SHA"].kind == "plain"


def test_example_optional_read_and_full_tree_patch_targets(tmp_path: Path) -> None:
    _write(tmp_path / "source", ".env.example", "SECRET_KEY=\n")
    _write(
        tmp_path / "source", "shared/env.py", 'import os\nsecret = os.getenv("SECRET_KEY", "")\n'
    )
    _write(tmp_path / "source", "shared/settings.py", 'app.secret_key = "dev"\n')
    out = _run(tmp_path, RunMode.BOOTSTRAP)
    assert len(out.env_keys) == 1
    assert not out.env_keys[0].required and out.env_keys[0].is_new
    assert out.env_keys[0].tier is None
    assert [(t.file, t.is_new, t.pattern_id) for t in out.patch_targets] == [
        ("shared/settings.py", True, "secret_key")
    ]


def test_ast_reads_ignore_comments_strings_stores_and_handle_aliases(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "import os as operating\nfrom os import environ as env, getenv as get\n"
        '# os.environ["COMMENT_TOKEN"]\n'
        "doc = 'os.getenv(\"STRING_TOKEN\")'\n"
        'os.environ["WRITE_TOKEN"] = "dev"\n'
        'operating.getenv(\n    "PORT",\n    "8080",\n)\n'
        'env.get("LOG_LEVEL", "info")\n'
        'get("DB_HOST")\n'
        'env["SECRET_KEY"]\n',
    )
    reads = rules.source_key_reads(tmp_path, ".")
    assert {name: required for name, _, _, required in reads} == {
        "PORT": False,
        "LOG_LEVEL": False,
        "DB_HOST": False,
        "SECRET_KEY": True,
    }
    old = rules.source_keys(tmp_path, ".")
    assert old == [(name, path, snippet) for name, path, snippet, _ in reads]
    assert all(len(item) == 3 and item[2] == f'os.getenv("{item[0]}")' for item in old)
    assert rules.source_keys(tmp_path, "../") == []


def test_manifest_excluded_env_reads_and_warning_targets(tmp_path: Path) -> None:
    for name in ["instance/a.py", "var/a.py", ".hidden/a.py", ".secrets/a.py"]:
        _write(tmp_path / "source", name, 'os.environ["HIDDEN_TOKEN"]\n')
    _write(tmp_path / "source", "tests/settings.py", 'SECRET_KEY = "dev"\n')
    _write(tmp_path / "source", "migrations/001.py", 'DB_HOST = "localhost"\n')
    out = _run(tmp_path)
    assert out.env_keys == ()
    assert len(out.patch_targets) == 2 and all(t.severity == "warning" for t in out.patch_targets)


def test_analysis_reports_non_python_and_does_not_read_large_binary(
    tmp_path: Path, monkeypatch
) -> None:
    from ddak.core.patch_patterns import MAX_SCAN_BYTES

    source = tmp_path / "source"
    _write(source, "app/settings.py", 'os.getenv("LOG_LEVEL")\n')
    _write(source, "static/client.js", 'const host = "http://localhost:8080";\n')
    large = source / "opaque.bin"
    with large.open("wb") as stream:
        stream.seek(MAX_SCAN_BYTES * 16)
        stream.write(b"\0")
    real_open = Path.open

    def checked_open(path, *args, **kwargs):
        assert path != large
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    out = _run(tmp_path)
    assert out.env_keys[0].name == "LOG_LEVEL" and not out.env_keys[0].required
    assert [(t.file, t.line, t.severity) for t in out.patch_targets] == [
        ("static/client.js", 1, "warning")
    ]
