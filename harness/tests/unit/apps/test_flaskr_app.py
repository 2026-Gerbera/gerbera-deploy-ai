"""Run with the isolated temp-box project, not the harness dependency environment."""

import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

try:
    from flaskr import create_app
    from flaskr.config import ConfigurationError, database_url
    from flaskr.db import user
    from flaskr.migrate import run
    from sqlalchemy import event, inspect, select, text
    from werkzeug.security import check_password_hash
except ModuleNotFoundError as error:
    if error.name != "flaskr":
        raise
    pytest.skip("Run this test in the isolated temp-box uv project", allow_module_level=True)

APP_ROOT = Path(__file__).resolve().parents[3] / "apps" / "temp-box"


@pytest.fixture
def environment(monkeypatch, tmp_path):
    values = {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'flaskr.db'}",
        "SECRET_KEY": secrets.token_hex(32),
        "APP_BASE_URL": "http://localhost",
        "SESSION_COOKIE_SECURE": "false",
        "PROXY_FIX_X_FOR": "0",
        "PROXY_FIX_X_PROTO": "0",
        "RELEASE_ID": "test-release",
    }
    monkeypatch.delenv("DATABASE_URL_MIGRATOR", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def app(environment):
    result = run("up")
    assert result["ok"], result
    app = create_app()
    app.config["TESTING"] = True
    yield app
    app.extensions["database"].dispose()


def credentials(username="alice"):
    return {"username": username, "password": "test-" + "passphrase"}


def sign_in(client, username="alice"):
    assert client.post("/auth/register", data=credentials(username)).status_code == 302
    assert client.post("/auth/login", data=credentials(username)).status_code == 302


def test_migration_lifecycle_is_non_destructive(environment):
    precheck = run("precheck")
    assert precheck["ok"] and precheck["current"] is None
    assert precheck["applied"] == []
    assert not run("verify")["ok"]
    first = run("up")
    assert first["ok"] and first["applied"] == ["0001"]
    app = create_app()
    engine = app.extensions["database"]
    with engine.begin() as conn:
        conn.execute(user.insert().values(username="preserved", password="hashed-value"))
    for phase in ("precheck", "up", "verify", "verify"):
        result = run(phase)
        assert result["ok"] and result["current"] == "0001"
        assert result["applied"] == []
        assert result["signature"] == first["signature"]
    with engine.connect() as conn:
        assert conn.execute(select(user.c.username)).scalar_one() == "preserved"
    engine.dispose()


@pytest.mark.parametrize("phase", ["precheck", "up", "verify"])
def test_cli_json_contract(environment, phase):
    assert run("up")["ok"]
    command = subprocess.run(
        [sys.executable, "-m", "flaskr.migrate", phase, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 0
    assert command.stderr == ""
    assert len(command.stdout.splitlines()) == 1
    result = json.loads(command.stdout)
    assert result["phase"] == phase and result["ok"] is True
    assert result["expected"] == result["current"] == "0001"
    assert result["applied"] == []
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", result["signature"])
    assert set(result["fingerprint"]) == {
        "version",
        "sql_mode",
        "collation",
        "time_zone",
        "ssl_version",
    }
    assert all(isinstance(value, str) for value in result["fingerprint"].values())


def test_auth_and_post_crud(app):
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/create").status_code == 302
    sign_in(client)
    assert b"already registered" in client.post("/auth/register", data=credentials()).data
    with app.extensions["database"].connect() as conn:
        password = conn.execute(select(user.c.password)).scalar_one()
    assert password != credentials()["password"]
    assert check_password_hash(password, credentials()["password"])
    assert client.get("/create").status_code == 200
    assert client.post("/create", data={"title": "", "body": "body"}).status_code == 200
    assert client.post("/create", data={"title": "first", "body": "<script>"}).status_code == 302
    assert b"&lt;script&gt;" in client.get("/").data
    assert client.get("/1/update").status_code == 200
    assert client.post("/1/update", data={"title": "changed", "body": "new"}).status_code == 302
    assert b"changed" in client.get("/").data
    assert client.get("/999/update").status_code == 404
    outsider = app.test_client()
    sign_in(outsider, "bob")
    assert outsider.get("/1/update").status_code == 403
    assert outsider.post("/1/delete").status_code == 403
    assert client.post("/1/delete").status_code == 302
    assert b"changed" not in client.get("/").data
    assert client.get("/auth/logout").status_code == 302
    assert client.get("/create").status_code == 302
    assert (
        b"Incorrect password"
        in client.post(
            "/auth/login",
            data={"username": "alice", "password": "wrong"},
        ).data
    )
    assert b"Incorrect username" in client.post("/auth/login", data=credentials("absent")).data


def test_health_version_and_no_runtime_ddl(app):
    statements = []
    event.listen(
        app.extensions["database"],
        "before_cursor_execute",
        lambda c, cu, s, p, co, e: statements.append(s),
    )
    client = app.test_client()
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json["current"] == "0001"
    assert client.get("/version").json == {"release_id": "test-release"}
    assert client.get("/static/style.css").status_code == 200
    assert not any(s.lstrip().upper().startswith(("CREATE", "DROP", "ALTER")) for s in statements)


def test_missing_schema_readiness_and_sanitized_response(environment, caplog):
    app = create_app()
    client = app.test_client()
    assert client.get("/health/ready").status_code == 503
    assert client.get("/version").status_code == 200
    assert client.get("/").json == {"error": "database_unavailable"}
    assert environment["DATABASE_URL"] not in caplog.text
    app.extensions["database"].dispose()


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE post RENAME COLUMN body TO missing_body",
        "UPDATE schema_version SET version='0002'",
        "DELETE FROM schema_version",
        "CREATE TABLE unexpected (id INTEGER)",
    ],
)
def test_schema_drift_fails_without_repair(app, ddl):
    with app.extensions["database"].begin() as conn:
        conn.execute(text(ddl))
    assert app.test_client().get("/health/ready").status_code == 503
    for phase in ("precheck", "up", "verify"):
        assert run(phase)["ok"] is False
    with app.extensions["database"].connect() as conn:
        assert "post" in inspect(conn).get_table_names()


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "SECRET_KEY",
        "APP_BASE_URL",
        "SESSION_COOKIE_SECURE",
        "PROXY_FIX_X_FOR",
        "PROXY_FIX_X_PROTO",
        "RELEASE_ID",
    ],
)
def test_all_configuration_is_required(environment, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ConfigurationError):
        create_app()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "not-a-database-url"),
        ("DATABASE_URL", "sqlite:///:memory:"),
        ("SECRET_KEY", "dev"),
        ("APP_BASE_URL", "http://user:password@localhost"),
        ("APP_BASE_URL", "http://localhost/path"),
        ("APP_BASE_URL", "http://localhost:bad"),
        ("SESSION_COOKIE_SECURE", "yes"),
        ("SESSION_COOKIE_SECURE", "true"),
        ("PROXY_FIX_X_FOR", "-1"),
        ("PROXY_FIX_X_PROTO", "5"),
    ],
)
def test_invalid_configuration(environment, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        create_app()


def test_migrator_precedence_and_runtime_rejection(environment, monkeypatch, tmp_path):
    selected = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL_MIGRATOR", f"sqlite:///{selected}")
    monkeypatch.setenv("DATABASE_URL", "invalid-and-unused")
    assert run("up")["ok"]
    assert selected.exists()
    with pytest.raises(ConfigurationError, match="Migration credentials"):
        create_app()


def test_mysql_requires_separate_migrator_url(environment, monkeypatch):
    password = secrets.token_hex(32)
    monkeypatch.setenv("DATABASE_URL", f"mysql+pymysql://flaskr_app:{password}@localhost/flaskr")
    assert database_url().username == "flaskr_app"
    with pytest.raises(ConfigurationError):
        database_url(migration=True)
    monkeypatch.setenv(
        "DATABASE_URL_MIGRATOR", f"mysql+pymysql://flaskr_migrator:{password}@localhost/flaskr"
    )
    assert database_url(migration=True).username == "flaskr_migrator"


def test_cli_failure_redacts_dsn(environment, monkeypatch):
    marker = secrets.token_hex(32)
    monkeypatch.setenv(
        "DATABASE_URL_MIGRATOR", f"mysql+pymysql://flaskr_migrator:{marker}@127.0.0.1:1/flaskr"
    )
    command = subprocess.run(
        [sys.executable, "-m", "flaskr.migrate", "precheck", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 1
    assert command.stderr == ""
    assert marker not in command.stdout
    assert "mysql+pymysql" not in command.stdout
    result = json.loads(command.stdout)
    assert result["ok"] is False and result["current"] is None
    assert result["error"] == "migration_failed"


@pytest.mark.parametrize(("hops", "expected_ip"), [("0", "127.0.0.1"), ("2", "198.51.100.8")])
def test_proxy_and_cookie_contract(environment, monkeypatch, hops, expected_ip):
    monkeypatch.setenv("APP_BASE_URL", "https://localhost")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("PROXY_FIX_X_FOR", hops)
    monkeypatch.setenv("PROXY_FIX_X_PROTO", "1" if hops == "2" else "0")
    assert run("up")["ok"]
    app = create_app()
    from flask import request

    @app.get("/probe")
    def probe():
        return {"ip": request.remote_addr, "scheme": request.scheme, "host": request.host}

    client = app.test_client()
    response = client.get(
        "/probe",
        base_url="http://localhost",
        headers={
            "X-Forwarded-For": "198.51.100.8, 192.0.2.10",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "attacker.invalid",
        },
    )
    assert response.json == {
        "ip": expected_ip,
        "scheme": "https" if hops == "2" else "http",
        "host": "localhost",
    }
    assert client.get("/", headers={"Host": "attacker.invalid"}).status_code == 400
    cookie = client.post("/auth/register", data={"username": "", "password": ""}).headers[
        "Set-Cookie"
    ]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    app.extensions["database"].dispose()


def test_official_source_and_build_inputs_preserved():
    provenance = json.loads((APP_ROOT / "upstream/provenance.json").read_text())
    for relative, digest in provenance["files_sha256"].items():
        assert hashlib.sha256((APP_ROOT / relative).read_bytes()).hexdigest() == digest
    assert (APP_ROOT / "Dockerfile").read_bytes() == (
        APP_ROOT / "docker/was.Dockerfile"
    ).read_bytes()
    assert (APP_ROOT / "images.lock.json").read_bytes() == (
        APP_ROOT.parents[1] / "var/validation/onprem-public-images.json"
    ).read_bytes()
    assert (APP_ROOT / "docker/mysql/10-init.sh").stat().st_mode & 0o111 == 0
    assert "mkdir -p /data && chown 65532:65532 /data" in (APP_ROOT / "Dockerfile").read_text()
    assert "EXPOSE 8080" in (APP_ROOT / "docker/web.Dockerfile").read_text()
    assert "listen 8080;" in (APP_ROOT / "docker/nginx/templates/default.conf.template").read_text()


def test_nginx_environment_validation():
    values = {
        **os.environ,
        "APP_BASE_URL": "https://example.test",
        "PUBLIC_HOST": "example.test",
        "PUBLIC_SCHEME": "https",
        "WAS_UPSTREAM": "192.168.10.2:8080",
        "TRUSTED_PROXY_CIDR": "172.18.0.2/32",
    }
    path = APP_ROOT / "docker/nginx/19-validate-env.sh"
    valid = subprocess.run(["sh", str(path)], env=values, capture_output=True, check=False)
    assert valid.returncode == 0
    for key, value in [
        ("PUBLIC_HOST", "bad;include injected"),
        ("WAS_UPSTREAM", "was:8000\ninclude /tmp/x;"),
        ("PUBLIC_SCHEME", "http"),
        ("WAS_UPSTREAM", "x:80;"),
        ("TRUSTED_PROXY_CIDR", "0.0.0.0/0"),
        ("TRUSTED_PROXY_CIDR", "999.0.0.1/32"),
    ]:
        result = subprocess.run(
            ["sh", str(path)], env={**values, key: value}, capture_output=True, check=False
        )
        assert result.returncode != 0
        assert value.encode() not in result.stderr


@pytest.mark.parametrize("invalid", [True, False])
def test_mysql_initialization_validates_and_sources_sql(tmp_path, invalid):
    app_password = secrets.token_hex(32)
    migration_password = secrets.token_hex(32)
    script = (APP_ROOT / "docker/mysql/10-init.sh").read_text()
    assert "/opt/ddak-init.sql.template" in script
    # Simulate only the provider's mount path and official entrypoint SQL function.
    local_script = tmp_path / "10-init.sh"
    local_script.write_text(
        script.replace(
            "/opt/ddak-init.sql.template",
            str(APP_ROOT / "docker/mysql/init.sql.template"),
        )
    )
    checker = tmp_path / "check_sql.py"
    checker.write_text(
        "import os, sys\n"
        "sql = sys.stdin.read()\n"
        "assert '@DDAK_' not in sql\n"
        "assert os.environ['DDAK_APP_PASSWORD'] in sql\n"
        "assert os.environ['DDAK_MIGRATION_PASSWORD'] in sql\n"
        "assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON `flaskr`.*' in sql\n"
        "assert 'GRANT ALL' not in sql and 'DROP' not in sql\n"
    )
    command = subprocess.run(
        [
            "bash",
            "-c",
            'set -e; docker_process_sql() { "$TEST_PYTHON" "$TEST_CHECKER"; }; source "$TEST_INIT"',
        ],
        env={
            **os.environ,
            "MYSQL_DATABASE": "flaskr",
            "DDAK_APP_PASSWORD": "invalid" if invalid else app_password,
            "DDAK_MIGRATION_PASSWORD": migration_password,
            "TEST_PYTHON": sys.executable,
            "TEST_CHECKER": str(checker),
            "TEST_INIT": str(local_script),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (command.returncode != 0) is invalid
    assert command.stdout == ""
    assert app_password not in command.stderr and migration_password not in command.stderr


def test_db_readiness_uses_tcp_and_hides_output(tmp_path):
    stub = tmp_path / "mysql"
    stub.write_text(
        "#!/bin/bash\n"
        "[[ \" $* \" == *' --protocol=TCP '* ]] || exit 5\n"
        "[[ \" $* \" == *' --host=127.0.0.1 '* ]] || exit 5\n"
        "[[ \" $* \" == *' --execute=SELECT 1 '* ]] || exit 5\n"
        '[[ "$MYSQL_PWD" == "$DDAK_APP_PASSWORD" ]] || exit 5\n'
        "printf '%s' \"$MYSQL_PWD\"\n"
    )
    stub.chmod(0o755)
    command = subprocess.run(
        ["bash", str(APP_ROOT / "docker/mysql/ready.sh")],
        env={
            **os.environ,
            "MYSQL_DATABASE": "flaskr",
            "DDAK_APP_PASSWORD": secrets.token_hex(32),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0
    assert command.stdout == command.stderr == b""


def test_gunicorn_sigterm_finishes_inflight_request(environment, tmp_path):
    assert run("up")["ok"]
    marker = tmp_path / "started"
    wrapper = tmp_path / "slow_app.py"
    wrapper.write_text(
        "import time\nfrom pathlib import Path\nfrom flaskr import create_app\n"
        "app = create_app()\n@app.get('/slow')\ndef slow():\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(0.5)\n    return 'finished'\n"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    dockerfile = (APP_ROOT / "docker/was.Dockerfile").read_text()
    command = json.loads(
        next(line[4:] for line in dockerfile.splitlines() if line.startswith("CMD "))
    )
    command[0:1] = [sys.executable, "-m", "gunicorn"]
    command[command.index("--bind") + 1] = f"127.0.0.1:{port}"
    command[command.index("--workers") + 1] = "1"
    command[-1] = "slow_app:app"

    def request_path(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path, headers={"Host": "localhost"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    process = subprocess.Popen(
        command, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                if request_path("/health/ready")[0] == 200:
                    break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("Gunicorn did not become ready")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request_path, "/slow")
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            process.terminate()
            assert future.result(timeout=5) == (200, b"finished")
        assert process.wait(timeout=8) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
