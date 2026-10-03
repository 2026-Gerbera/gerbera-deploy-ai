"""수정11: 전체 build 트리 AST 탐지와 값 없는 계약."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ddak.core.contracts.plan_facts import EnvKey, Facts, PatchTarget
from ddak.core.contracts.tools.analyze_project import AnalyzeProjectOutput
from ddak.core.patch_patterns import MAX_SCAN_BYTES, PATTERNS, scan_patch_targets
from ddak.plan.patch import check


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_full_tree_and_supported_literal_sites(tmp_path: Path) -> None:
    secret = "demo" + "-signing-value"
    _write(
        tmp_path,
        "outside/config.py",
        f'app.secret_key = "{secret}"\n'
        'app.config["SESSION_COOKIE_SECURE"] = False\n'
        'DATABASE_URL = "mysql+pymysql://dev:fake@127.0.0.1/demo"\n'
        'SERVICE_URL = "http://localhost:8080/path"\n'
        'DB_HOST = "localhost"\n'
        "ProxyFix(app.wsgi_app, x_for=1, x_proto=0)\n",
    )
    targets = scan_patch_targets(tmp_path, ())
    assert [(t.line, t.pattern_id, t.key) for t in targets] == [
        (1, "secret_key", "SECRET_KEY"),
        (2, "cookie_secure", "SESSION_COOKIE_SECURE"),
        (3, "local_address", "DATABASE_URL"),
        (4, "local_address", "SERVICE_URL"),
        (5, "local_address", "DB_HOST"),
        (6, "proxy_fix", "PROXY_FIX_X_FOR"),
        (6, "proxy_fix", "PROXY_FIX_X_PROTO"),
    ]
    assert all(
        t.file == "outside/config.py" and not t.is_new and t.severity == "patch" for t in targets
    )
    serialized = "".join(t.model_dump_json() for t in targets)
    assert secret not in serialized and "fake@" not in serialized and "snippet" not in serialized
    assert check.PATTERNS is PATTERNS


@pytest.mark.parametrize(
    "changed,bootstrap,new", [((), False, False), (("a.py",), False, True), ((), True, True)]
)
def test_new_marking(tmp_path: Path, changed: tuple[str, ...], bootstrap: bool, new: bool) -> None:
    _write(tmp_path, "a.py", 'SECRET_KEY = "dev"\n')
    assert scan_patch_targets(tmp_path, changed, bootstrap=bootstrap)[0].is_new is new


@pytest.mark.parametrize(
    "host,severity",
    [
        ("localhost", "patch"),
        ("LOCALHOST.", "patch"),
        ("127.0.0.2", "patch"),
        ("::1", "patch"),
        ("http://[::1]:8080/path", "patch"),
        ("mysql://dev:fake@localhost/db", "patch"),
        ("10.2.3.4", "warning"),
        ("http://172.16.1.2:8080", "warning"),
        ("mysql://192.168.2.3/db", "warning"),
        ("fd00::1", "warning"),
        ("https://example.com/localhost", None),
        ("localhost.example.com", None),
        ("visit http://localhost:80", None),
        ("http://[invalid", None),
        ("8.8.8.8", None),
    ],
)
def test_address_classification(tmp_path: Path, host: str, severity: str | None) -> None:
    _write(tmp_path, "a.py", f"HOST = {host!r}\n")
    found = scan_patch_targets(tmp_path, ())
    assert [t.severity for t in found] == ([] if severity is None else [severity])


@pytest.mark.parametrize(
    "name",
    [
        "tests/a.py",
        "migrations/001.py",
        "nested/test_config.py",
        "nested/config_test.py",
        "conftest.py",
    ],
)
def test_test_and_migration_targets_warning_only(tmp_path: Path, name: str) -> None:
    _write(tmp_path, name, 'SECRET_KEY = "dev"\nSESSION_COOKIE_SECURE = True\n')
    assert {t.severity for t in scan_patch_targets(tmp_path, (name,))} == {"warning"}


def test_ast_skips_comments_docstrings_dynamic_values_and_wrong_types(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.py",
        '"""http://localhost:80"""\n'
        '# SECRET_KEY = "dev"\n'
        'text = "SECRET_KEY = dev; http://localhost"\n'
        'SECRET_KEY = os.environ["SECRET_KEY"]\n'
        'app.secret_key = os.getenv("SECRET_KEY")\n'
        'SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE")\n'
        "SESSION_COOKIE_SECURE = 1\n"
        "ProxyFix(app, x_for=True, x_proto=count)\n"
        "unrelated(app, x_for=1, x_proto=0)\n"
        'def example():\n    """localhost"""\n    pass\n',
    )
    assert scan_patch_targets(tmp_path, ()) == ()


def test_dict_keyword_annotated_assignment_and_multiline_proxy(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.py",
        'SECRET_KEY: str = "dev"\n'
        'settings = {"SECRET_KEY": "dev", "SESSION_COOKIE_SECURE": True}\n'
        'app.config.update(SECRET_KEY="dev", SESSION_COOKIE_SECURE=False)\n'
        "from werkzeug.middleware.proxy_fix import ProxyFix as PF\n"
        "PF(\n    app,\n    x_for=2,\n    x_proto=-1,\n)\n",
    )
    found = scan_patch_targets(tmp_path, ())
    assert len(found) == 7
    assert {t.line for t in found if t.pattern_id == "proxy_fix"} == {7, 8}


def test_manifest_exclusions_and_hidden_sources(tmp_path: Path) -> None:
    for name in [
        ".env",
        ".env.production",
        ".env.example",
        ".secrets/a.py",
        "var/a.py",
        "instance/a.py",
        "node_modules/a.py",
        ".hidden/a.py",
    ]:
        _write(tmp_path, name, 'SECRET_KEY = "must-not-scan"\n')
    _write(tmp_path, "real.py", 'SECRET_KEY = "dev"\n')
    _write(tmp_path, "invalid.py", "not valid python !")
    assert [t.file for t in scan_patch_targets(tmp_path, ())] == ["real.py"]


def test_manifest_rejects_symlinks(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", 'SECRET_KEY = "dev"\n')
    (tmp_path / "linked.py").symlink_to(tmp_path / "a.py")
    with pytest.raises(ValueError, match="심볼릭"):
        scan_patch_targets(tmp_path, ())


def test_contract_defaults_and_no_value_fields() -> None:
    assert EnvKey(name="PORT", kind="plain").required is True
    target = PatchTarget(
        file="a.py", line=1, pattern_id="secret_key", is_new=False, severity="patch"
    )
    assert target.key is None
    assert set(PatchTarget.model_fields) == {
        "file",
        "line",
        "pattern_id",
        "is_new",
        "severity",
        "key",
    }
    assert Facts.model_fields["patch_targets"].default == ()
    output = AnalyzeProjectOutput(
        tiers=(), env_keys=(), has_dockerfile={}, infra_inputs_changed=False
    )
    assert output.patch_targets == ()
    with pytest.raises(ValidationError):
        PatchTarget.model_validate({**target.model_dump(), "snippet": "secret"})
    with pytest.raises(ValidationError):
        PatchTarget.model_validate({**target.model_dump(), "severity": "unsafe"})


def test_non_python_full_tree_warning_locations_without_values(tmp_path: Path) -> None:
    secret = "nonpython" + "-fake-secret"
    _write(
        tmp_path,
        "templates/index.html",
        '<html>\n<a href="http://localhost:8080">app</a>\n</html>\n',
    )
    _write(
        tmp_path, "static/client.js", f'const SECRET_KEY = "{secret}";\nconst host = "10.1.2.3";\n'
    )
    _write(tmp_path, "config/settings.yaml", "SESSION_COOKIE_SECURE: false\nx_for: 1\n")
    _write(tmp_path, "other/build-settings", "DB_HOST=127.0.0.1\n")
    _write(tmp_path, ".secrets/settings.yaml", "SECRET_KEY=excluded\n")
    targets = scan_patch_targets(tmp_path, ("static/client.js",))
    assert [(t.file, t.line, t.pattern_id) for t in targets] == [
        ("config/settings.yaml", 1, "cookie_secure"),
        ("config/settings.yaml", 2, "proxy_fix"),
        ("other/build-settings", 1, "local_address"),
        ("static/client.js", 1, "secret_key"),
        ("static/client.js", 2, "local_address"),
        ("templates/index.html", 2, "local_address"),
    ]
    assert all(t.severity == "warning" and t.key is None for t in targets)
    assert all(t.is_new == (t.file == "static/client.js") for t in targets)
    assert secret not in "".join(t.model_dump_json() for t in targets)
    assert all(t.is_new for t in scan_patch_targets(tmp_path, (), bootstrap=True))


def test_binary_and_oversize_files_are_not_unbounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "a.py", 'SECRET_KEY = "dev"\n')
    (tmp_path / "binary.dat").write_bytes(b"\x00SECRET_KEY=never-a-target\n")
    (tmp_path / "invalid.dat").write_bytes(b"\xffSECRET_KEY=never-a-target\n")
    large = tmp_path / "large.dat"
    with large.open("wb") as stream:
        stream.seek(MAX_SCAN_BYTES * 16)
        stream.write(b"\0")
    real_open = Path.open

    def checked_open(path: Path, *args, **kwargs):
        assert path != large, "상한을 넘는 바이너리는 열지 않아야 한다"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    assert [t.file for t in scan_patch_targets(tmp_path, ())] == ["a.py"]


def test_supported_python_oversize_fails_closed(tmp_path: Path) -> None:
    with (tmp_path / "large.py").open("wb") as stream:
        stream.seek(MAX_SCAN_BYTES + 1)
        stream.write(b"\n")
    with pytest.raises(ValueError, match=r"Python 탐지 크기 상한 초과: large\.py"):
        scan_patch_targets(tmp_path, ())


def test_file_growth_still_uses_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    _write(tmp_path, "grown.txt", "small\n")
    read_sizes: list[int] = []

    class GrowingFile(BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            assert size == MAX_SCAN_BYTES + 1
            return b"x" * size

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: GrowingFile())
    assert scan_patch_targets(tmp_path, ()) == ()
    assert read_sizes == [MAX_SCAN_BYTES + 1]
