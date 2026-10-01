"""source=fake: 실제 SSH 연결 없이 VM 경계·replica 실패·복구를 검증한다."""

from __future__ import annotations

import base64
import copy
import hashlib
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy import OnPremProvider
from ddak.onprem.deploy.containers import DockerHost
from tests.unit.onprem.deploy.test_onprem import FakeDocker

BLOB = base64.b64encode(b"fixture-host-public-key").decode()
PIN = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(BLOB)).digest()).decode().rstrip(
    "="
)


class FakeVM(FakeDocker):
    def __init__(self):
        super().__init__()
        self.argvs = []
        self.tempdirs = set()
        self.ssh_failure = None
        self.bad_pin = False
        self.config_texts = []
        self.known_texts = []
        self.fail_ready = None
        self.timeout_prefix = None
        self.ready_names = []

    def __call__(self, argv, *, timeout):
        self.argvs.append(argv)
        if "-F" in argv:
            cfg = Path(argv[argv.index("-F") + 1])
            self.tempdirs.add(cfg.parent)
            assert stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700
            for f in (cfg, cfg.parent / "known_hosts"):
                assert stat.S_IMODE(f.stat().st_mode) == 0o600
            self.config_texts.append(cfg.read_text())
            self.known_texts.append((cfg.parent / "known_hosts").read_text())
            if self.ssh_failure == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout, stderr="private")
            if self.ssh_failure == "exit":
                return subprocess.CompletedProcess(argv, 255, "", "private")
        if "ssh-keyscan" in argv:
            key = base64.b64encode(b"incorrect").decode() if self.bad_pin else BLOB
            return subprocess.CompletedProcess(argv, 0, f"host ssh-ed25519 {key}\n", "")
        if argv[-1] == "true":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "/usr/bin/env":
            assert argv[1:5] == ["-u", "DOCKER_HOST", "-u", "DOCKER_CONTEXT"]
            directory = Path(argv[5].removeprefix("PATH=").split(os.pathsep)[0])
            self.tempdirs.add(directory)
            assert stat.S_IMODE((directory / "ssh").stat().st_mode) == 0o700
            wrapper = (directory / "ssh").read_text()
            assert "-F " in wrapper and '"$@"' in wrapper
            assert not any(
                x.startswith(("HOME=", "DOCKER_CONFIG=", "SSH_AUTH_SOCK=")) for x in argv
            )
            argv = argv[6:]
            if argv[1:3] == ["buildx", "imagetools"]:
                argv = ["docker", "--host", "unused-for-fixture", *argv[1:]]
            else:
                assert argv[:3] == ["docker", "--host", "ssh://ddak-target"]
        args = argv[3:]
        if self.timeout_prefix and args[: len(self.timeout_prefix)] == self.timeout_prefix:
            raise subprocess.TimeoutExpired(argv, timeout, stderr="private")
        if args[:2] == ["container", "exec"]:
            state = self.find(args[2])
            name = next(n for n, v in self.containers.items() if v is state)
            self.ready_names.append(name)
            if name == self.fail_ready and state["Config"]["Image"] == self.refs[1]:
                return subprocess.CompletedProcess(argv, 1, "", "private")
        return super().__call__(argv, timeout=timeout)


@pytest.fixture
def vm(tmp_path):
    key = tmp_path / "test-identity"
    key.touch(mode=0o600)
    fake = FakeVM()
    inventory = {
        "mode": "vm",
        "public_url": "https://demo.example.invalid",
        "ssh": {
            "host": "was.example.invalid",
            "user": "deploy",
            "key_path": str(key),
            "host_key_fingerprint": PIN,
        },
        "tiers": {
            "was": {
                "name": "app",
                "platform": "linux/arm64",
                "network": "demo-net",
                "env_file": str(tmp_path / "was.env"),
            }
        },
    }
    ctx = RunContext(
        "unit-run", project="flaskr", images={"was": fake.refs[0]}, platform={"onprem": inventory}
    )
    provider = OnPremProvider(fake)
    provider.inject_config([], ctx)
    assert fake.argvs == []  # 설정 주입은 SSH/키 stat도 필요 없다.
    return fake, provider, ctx


def test_vm_endpoint_config_pins_and_cleanup(vm, monkeypatch):
    fake, provider, ctx = vm
    key = ctx.platform["onprem"]["ssh"]["key_path"]
    original = Path.open

    def no_key_read(path, *args, **kwargs):
        assert str(path) != key
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_key_read)
    provider.deploy("was", ctx)
    assert fake.config_texts
    cfg = fake.config_texts[-1]
    for expected in (
        "Host ddak-target",
        "StrictHostKeyChecking yes",
        "IdentitiesOnly yes",
        "BatchMode yes",
        "GlobalKnownHostsFile /dev/null",
        "HostKeyAlias ddak-target",
    ):
        assert expected in cfg
    assert "ControlPersist" not in cfg
    assert f"ddak-target ssh-ed25519 {BLOB}" in fake.known_texts[-1]
    assert all(not path.exists() for path in fake.tempdirs)
    manifests = [a for a in fake.argvs if "imagetools" in a]
    assert manifests and all("--host" not in a for a in manifests)


def test_vm_jump_scans_through_pinned_jump(vm):
    fake, provider, ctx = vm
    ctx.platform["onprem"]["ssh"]["jump_host"] = {
        "host": "jump.example.invalid",
        "port": 2222,
        "user": "jump",
        "host_key_fingerprint": PIN,
    }
    provider.deploy("was", ctx)
    scans = [a for a in fake.argvs if "ssh-keyscan" in a]
    assert scans[0] == ["ssh-keyscan", "-T", "5", "-p", "2222", "jump.example.invalid"]
    assert "ddak-jump" in scans[1] and "-F" in scans[1]
    assert "ProxyJump ddak-jump" in fake.config_texts[-1]
    assert all(alias in fake.known_texts[-1] for alias in ("ddak-jump", "ddak-target"))
    assert all(not path.exists() for path in fake.tempdirs)


@pytest.mark.parametrize("failure", ["timeout", "exit", "pin", "docker"])
def test_vm_failure_is_sanitized_and_temporary_files_removed(vm, failure, monkeypatch, tmp_path):
    fake, provider, ctx = vm
    import ddak.onprem.deploy.ssh as ssh_module

    made = tmp_path / "ssh-work"

    def mkdtemp(*, prefix):
        made.mkdir(mode=0o700)
        return str(made)

    monkeypatch.setattr(ssh_module.tempfile, "mkdtemp", mkdtemp)
    if failure == "pin":
        fake.bad_pin = True
    elif failure == "docker":
        fake.fail_prefix = ["image", "pull"]
    else:
        fake.ssh_failure = failure
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("was", ctx)
    assert caught.value.code == (
        ErrorCode.CONFIG_INVALID if failure == "pin" else ErrorCode.ADAPTER_FAILED
    )
    assert "private" not in str(caught.value) and str(tmp_path) not in str(caught.value)
    assert not fake.containers and not made.exists()


@pytest.mark.parametrize(
    "case",
    [
        "ssh-in-container",
        "missing-ssh",
        "endpoint",
        "ports",
        "bridge",
        "host",
        "user",
        "pin",
        "key-perm",
        "key-symlink",
        "extra",
        "replicas",
        "replicas-ports",
        "label",
        "network-label",
        "ready",
    ],
)
def test_invalid_inventory_and_key_rejected_before_docker(vm, case, tmp_path):
    fake, provider, ctx = vm
    inv = copy.deepcopy(ctx.platform["onprem"])
    tier = inv["tiers"]["was"]
    if case == "ssh-in-container":
        inv["mode"] = "container"
    elif case == "missing-ssh":
        del inv["ssh"]
    elif case == "endpoint":
        inv["docker_host"] = "unix:///anything"
    elif case == "ports":
        tier["ports"] = ["127.0.0.1:1234:8000"]
    elif case == "bridge":
        tier["network"] = "bridge"
    elif case == "host":
        inv["ssh"]["host"] = "host;id"
    elif case == "user":
        inv["ssh"]["user"] = "-oProxyCommand=x"
    elif case == "pin":
        inv["ssh"]["host_key_fingerprint"] = "anything"
    elif case == "key-perm":
        Path(inv["ssh"]["key_path"]).chmod(0o644)
    elif case == "key-symlink":
        link = tmp_path / "key-link"
        link.symlink_to(inv["ssh"]["key_path"])
        inv["ssh"]["key_path"] = str(link)
    elif case == "extra":
        inv["ssh"]["password"] = "fake"
    elif case == "replicas":
        tier["replicas"] = 0
    elif case == "replicas-ports":
        inv.pop("ssh")
        inv["mode"] = "container"
        tier["replicas"] = 2
        tier["ports"] = ["127.0.0.1:1234:8000"]
    elif case == "label":
        tier["traefik_labels"] = {"traefik.enable": "true\ninjected"}
    elif case == "network-label":
        tier["traefik_labels"] = {"traefik.docker.network": "other"}
    elif case == "ready":
        tier["ready"] = {"path": "//other"}
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("was", replace(ctx, platform={"onprem": inv}))
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert not fake.argvs


def test_env_remote_endpoint_stays_rejected(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setenv("DOCKER_HOST", "ssh://unapproved")
    with pytest.raises(DdakToolError):
        DockerHost(fake).run("info")
    assert not fake.calls


def _replicas(vm):
    fake, provider, ctx = vm
    ctx.platform["onprem"]["tiers"]["was"].update(
        {
            "replicas": 3,
            "traefik_labels": {
                "traefik.enable": "true",
                "traefik.http.routers.app.rule": "PathPrefix(`/`)",
            },
        }
    )
    provider.deploy("was", ctx)
    baseline = {name: data["Id"] for name, data in fake.containers.items()}
    return (
        fake,
        provider,
        replace(
            ctx,
            images={"was": fake.refs[1]},
            previous_release={"local": {"images": {"was": fake.refs[0]}}},
        ),
        baseline,
    )


def test_three_replicas_sequential_ready_single_pull_and_health(vm):
    fake, provider, ctx, _ = _replicas(vm)
    mark = len(fake.calls)
    fake.ready_names.clear()
    result = provider.deploy("was", ctx)
    assert result.changed and result.observation.platform_digest == fake.children[1]
    assert fake.ready_names == ["app-1", "app-2", "app-3"]
    assert sum(a[:2] == ["image", "pull"] for a in fake.calls[mark:]) == 1
    for i in range(1, 4):
        labels = fake.containers[f"app-{i}"]["Config"]["Labels"]
        assert labels["ddak.replica"] == str(i)
        assert labels["traefik.docker.network"] == "demo-net"
    assert provider.health_check(ctx).passed
    fake.containers["app-2"]["RestartCount"] = 1
    with pytest.raises(DdakToolError, match="실행 관측"):
        provider.health_check(ctx)


def test_second_replica_readiness_failure_rolls_back_only_changed_and_is_idempotent(vm):
    fake, provider, ctx, baseline = _replicas(vm)
    fake.fail_ready = "app-2"
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", ctx)
    assert error.value.code == ErrorCode.ADAPTER_FAILED
    assert fake.containers["app-3"]["Id"] == baseline["app-3"]
    result = provider.rollback("was", ctx)
    assert result.detail == "복구 replica: [1, 2]"
    assert all(v["Config"]["Image"] == fake.refs[0] for v in fake.containers.values())
    assert fake.containers["app-3"]["Id"] == baseline["app-3"]
    assert not provider.rollback("was", ctx).changed


def test_missing_replica_and_staged_are_restored(vm):
    fake, provider, ctx, baseline = _replicas(vm)
    # 두 번째 replica의 기존 컨테이너 제거 뒤 rename 직전 실패를 재현한다.
    original = fake.__call__

    def runner(argv, *, timeout):
        if "rename" in argv and argv[-1] == "app-2":
            return subprocess.CompletedProcess(argv, 1, "", "private")
        return original(argv, timeout=timeout)

    provider.runner = runner
    with pytest.raises(DdakToolError):
        provider.deploy("was", ctx)
    assert "app-2" not in fake.containers and "app-2-ddak-next" in fake.containers
    provider.runner = fake
    assert provider.rollback("was", ctx).detail == "복구 replica: [1, 2]"
    assert fake.containers["app-3"]["Id"] == baseline["app-3"]
    assert not any(n.endswith("-ddak-next") for n in fake.containers)


def test_no_previous_cleans_all_owned_replicas_and_staged(vm):
    fake, provider, ctx, _ = _replicas(vm)
    fake.containers["app-2-ddak-next"] = fake.containers.pop("app-2")
    ctx = replace(ctx, previous_release={})
    assert provider.rollback("was", ctx).changed
    assert not fake.containers
    assert not provider.rollback("was", ctx).changed


def test_foreign_replica_prevents_all_mutation(vm):
    fake, provider, ctx, _ = _replicas(vm)
    fake.containers["app-3"]["Config"]["Labels"]["ddak.project"] = "someone-else"
    mark = len(fake.calls)
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", ctx)
    assert error.value.code == ErrorCode.PRECONDITION_FAILED
    assert not any(
        a[:2] in (["container", "create"], ["container", "stop"]) for a in fake.calls[mark:]
    )


@pytest.mark.parametrize(
    "prefix,code",
    [
        (["image", "pull"], ErrorCode.ADAPTER_FAILED),
        (["image", "inspect"], ErrorCode.ADAPTER_FAILED),
        (["container", "create"], ErrorCode.ADAPTER_TIMEOUT),
        (["container", "rename"], ErrorCode.ADAPTER_TIMEOUT),
        (["container", "exec"], ErrorCode.ADAPTER_TIMEOUT),
    ],
)
def test_pre_mutation_timeout_vs_unknown_state(vm, prefix, code):
    fake, provider, ctx = vm
    fake.timeout_prefix = prefix
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", ctx)
    assert error.value.code == code
    assert all(not path.exists() for path in fake.tempdirs)


def test_container_mode_can_opt_in_to_replicas(vm):
    fake, provider, ctx = vm
    inv = copy.deepcopy(ctx.platform["onprem"])
    inv.pop("ssh")
    inv["mode"] = "container"
    inv["docker_host"] = "unix:///test.sock"
    inv["tiers"]["was"].update({"replicas": 1, "ready": {"timeout_s": 1.0}})
    ctx = replace(ctx, platform={"onprem": inv})
    provider.deploy("was", ctx)
    assert "app-1" in fake.containers and "app" not in fake.containers
    assert fake.ready_names == ["app-1"]
    assert not fake.tempdirs


def test_secret_is_created_only_when_requested_and_never_returned(vm):
    fake, provider, ctx = vm
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    assert "SECRET_KEY" not in path.read_text()
    assert not provider.inject_config([], ctx).changed
    first = provider.inject_config(["SECRET_KEY"], ctx)
    original = path.read_text()
    assert "SECRET_KEY=" in original
    assert not provider.inject_config(["SECRET_KEY"], ctx).changed
    assert path.read_text() == original
    value = original.split("SECRET_KEY=", 1)[1].strip()
    assert value not in first.model_dump_json() and value not in str(ctx.to_json_dict())
    assert not fake.argvs


@pytest.mark.parametrize(
    "key,value,valid",
    [
        ("APP_ENV", "production", True),
        ("PROXY_FIX_X_FOR", "2", True),
        ("PROXY_FIX_X_PROTO", "0", True),
        ("PROXY_FIX_X_PROTO", "5", True),
        ("PROXY_FIX_X_FOR", "6", False),
        ("PROXY_FIX_X_FOR", "1.0", False),
        ("PROXY_FIX_X_FOR", "-1", False),
        ("SESSION_COOKIE_SECURE", "true", True),
        ("SESSION_COOKIE_SECURE", "1", False),
    ],
)
def test_public_proxy_settings_are_values_not_fixed_hops(vm, key, value, valid):
    _, provider, ctx = vm
    ctx.platform["onprem"]["tiers"]["was"]["public_env"] = {key: value}
    if valid:
        result = provider.inject_config([], ctx)
        assert result.keys == [key]
    else:
        with pytest.raises(DdakToolError):
            provider.inject_config([], ctx)


def test_secure_cookie_http_is_rejected(vm):
    fake, provider, ctx = vm
    ctx.platform["onprem"]["public_url"] = "http://example.invalid"
    ctx.platform["onprem"]["tiers"]["was"]["public_env"] = {"SESSION_COOKIE_SECURE": "true"}
    with pytest.raises(DdakToolError) as error:
        provider.inject_config([], ctx)
    assert error.value.code == ErrorCode.CONFIG_INVALID and not fake.argvs


def test_same_image_new_release_recreates_and_rollback_restores_release_id(vm):
    fake, provider, ctx = vm
    provider.deploy("was", ctx)
    first = fake.containers["app"]["Id"]
    update = replace(
        ctx,
        run_id="another-run",
        previous_release={"local": {"release_id": ctx.run_id, "images": dict(ctx.images)}},
    )
    assert provider.deploy("was", update).changed
    assert fake.containers["app"]["Id"] != first
    creates = [a for a in fake.calls if a[:2] == ["container", "create"]]
    assert "RELEASE_ID=another-run" in creates[-1]
    assert provider.rollback("was", update).changed
    creates = [a for a in fake.calls if a[:2] == ["container", "create"]]
    assert f"RELEASE_ID={ctx.run_id}" in creates[-1]
    assert not provider.rollback("was", update).changed


def test_env_key_set_change_recreates_same_release(vm):
    fake, provider, ctx = vm
    provider.deploy("was", ctx)
    provider.inject_config(["SECRET_KEY"], ctx)
    assert provider.deploy("was", ctx).changed
    assert not provider.deploy("was", ctx).changed
    assert "SECRET_KEY" not in str(fake.calls)  # 값 대신 env-file 경로만 전달


def test_vm_migration_uses_one_ssh_session_and_no_traefik_labels(vm):
    fake, provider, ctx = vm
    ctx.platform["onprem"]["tiers"]["was"].update(
        {
            "replicas": 3,
            "traefik_labels": {"traefik.enable": "true"},
        }
    )
    result = provider.migrate_db(["001_users"], ctx)
    assert result.changed and result.migration["event"] == "MIGRATE_RESULT"
    assert [p["phase"] for p in result.migration["phases"]] == ["precheck", "up", "verify"]
    assert sum(a[-1] == "true" for a in fake.argvs) == 1
    creates = [a for a in fake.calls if a[:2] == ["container", "create"]]
    assert len(creates) == 3
    for args in creates:
        assert "--rm" not in args and "--env-file" in args and "demo-net" in args
        assert not any("traefik." in a or "ddak.replica" in a for a in args)
        assert args[-4:] == ["-m", "flaskr.migrate", args[-2], "--json"]
    assert not fake.containers and all(not p.exists() for p in fake.tempdirs)


@pytest.mark.parametrize("case", ["false", "extra", "current", "timeout"])
def test_vm_migration_invalid_result_and_timeout_cleanup(vm, case):
    import json

    fake, provider, ctx = vm
    if case == "false":
        fake.migration_status = "failed"
    elif case == "timeout":
        fake.wait_timeout = True
    else:
        # 출력은 fake runner가 사용하는 정상 모양을 만들고 실제 검증 필드를 바꾼다.
        original = fake.__call__

        def runner(argv, *, timeout):
            result = original(argv, timeout=timeout)
            if "logs" in argv:
                obj = json.loads(result.stdout.removeprefix("MIGRATE_RESULT "))
                if case == "extra":
                    obj["extra"] = "unexpected"
                elif obj["phase"] == "verify":
                    obj["current"] = "0000"
                result.stdout = "MIGRATE_RESULT " + json.dumps(obj)
            return result

        provider.runner = runner
    with pytest.raises(DdakToolError) as caught:
        provider.migrate_db(["001_users"], ctx)
    assert caught.value.code == (
        ErrorCode.ADAPTER_TIMEOUT if case == "timeout" else ErrorCode.ADAPTER_FAILED
    )
    assert not fake.containers
    assert all(not p.exists() for p in fake.tempdirs)


@pytest.mark.parametrize(
    "failure", [None, "api", "client", "arch", "network", "foreign", "env", "key", "pin"]
)
def test_preflight_checks_without_mutation(vm, failure):
    import json

    from ddak.onprem.deploy import preflight_inventory

    fake, provider, ctx = vm
    inv = ctx.platform["onprem"]
    if failure == "foreign":
        provider.deploy("was", ctx)
        fake.containers["app"]["Config"]["Labels"]["ddak.project"] = "foreign"
    if failure == "env":
        Path(inv["tiers"]["was"]["env_file"]).chmod(0o644)
    if failure == "key":
        Path(inv["ssh"]["key_path"]).chmod(0o644)
    if failure == "pin":
        fake.bad_pin = True
    mark = len(fake.calls)
    original = fake.__call__

    def runner(argv, *, timeout):
        if "version" in argv:
            value = {
                "Server": {
                    "ApiVersion": "1.48" if failure == "api" else "1.49",
                    "Os": "linux",
                    "Arch": "amd64" if failure == "arch" else "arm64",
                },
                "Client": {"ApiVersion": "1.48" if failure == "client" else "1.49"},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if "network" in argv:
            return subprocess.CompletedProcess(
                argv, 1 if failure == "network" else 0, json.dumps("demo-net"), "private"
            )
        return original(argv, timeout=timeout)

    report = preflight_inventory(inv, project=ctx.project, runner=runner)
    assert report["passed"] == (failure is None)
    assert all(c["status"] in {"ok", "fail", "skip"} for c in report["checks"])
    assert not any(
        a[:2] in (["image", "pull"], ["container", "create"], ["container", "rm"])
        for a in fake.calls[mark:]
    )
    assert "private" not in str(report)
    assert inv["ssh"]["key_path"] not in str(report)
    assert all(not p.exists() for p in fake.tempdirs)


def test_preflight_bad_inventory_has_no_runner_calls():
    from ddak.onprem.deploy import preflight_inventory

    fake = FakeVM()
    result = preflight_inventory({"mode": "vm", "tiers": {}}, runner=fake)
    assert not result["passed"] and not fake.argvs
