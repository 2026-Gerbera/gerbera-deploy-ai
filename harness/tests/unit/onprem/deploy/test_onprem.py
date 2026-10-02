"""source=fake: Docker 명령 경계/실패 불변식 검사. 실제 증명은 tests/docker에서 별도 실행."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ddak.cd.interface import ProviderResult
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.onprem.deploy import OnPremProvider
from ddak.onprem.deploy.containers import DockerHost, subprocess_runner


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class FakeDocker:
    source = "fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.containers: dict[str, dict] = {}
        self.manifests = {}
        self.images = {}
        self.refs = []
        self.children = []
        self.native = False
        self.volume_options = None
        self.fail_prefix: list[str] | None = None
        self.migration_status = "ok"
        self.migration_override: str | None = None
        self.wait_timeout = False
        for version in ("v1", "v2"):
            config_id = digest("config-" + version)
            child = json.dumps({"schemaVersion": 2, "config": {"digest": config_id}})
            child_digest = digest(child)
            index = json.dumps(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {
                            "digest": child_digest,
                            "platform": {"os": "linux", "architecture": "arm64"},
                        }
                    ],
                }
            )
            ref = "example/app@" + digest(index)
            self.refs.append(ref)
            self.children.append(child_digest)
            self.manifests[ref] = index
            self.manifests["example/app@" + child_digest] = child
            self.images[ref] = {
                "Id": config_id,
                "Os": "linux",
                "Architecture": "arm64",
                "Volumes": None,
            }

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list)
        assert timeout > 0
        self.timeouts.append(timeout)
        if argv[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "unix:///test.sock\n", "")
        args = argv[3:]
        self.calls.append(args)
        if self.fail_prefix and args[: len(self.fail_prefix)] == self.fail_prefix:
            return subprocess.CompletedProcess(argv, 1, "", "sensitive stderr should never escape")
        out = ""
        if args[:2] == ["image", "inspect"]:
            out = json.dumps(self.images[args[-1]])
        elif args[:3] == ["buildx", "imagetools", "inspect"]:
            out = self.manifests[args[-1]] + "\n"
        elif args[:2] == ["container", "ls"]:
            pattern = args[-1].removeprefix("name=")
            out = "\n".join(
                value["Id"]
                for name, value in self.containers.items()
                if re.fullmatch(pattern, "/" + name)
            )
        elif args[:2] == ["container", "inspect"]:
            out = json.dumps(self.find(args[-1]))
        elif args[:2] == ["container", "create"]:
            name = args[args.index("--name") + 1]
            assert name not in self.containers
            ref = next(item for item in args if "@sha256:" in item)
            labels = dict(
                args[i + 1].split("=", 1) for i, item in enumerate(args) if item == "--label"
            )
            self.containers[name] = {
                "Id": digest(name + str(len(self.calls)))[7:],
                "Image": self.images[ref]["Id"],
                "Config": {"Image": ref, "Labels": labels},
                "State": {"Running": False},
                "phase": args[-2] if "flaskr.migrate" in args else None,
            }
            if "--health-cmd" in args:
                self.containers[name]["State"]["Health"] = {"Status": "healthy"}
            if self.native:
                self.containers[name]["ImageManifestDescriptor"] = self.images[ref]["Descriptor"]
        elif args[:2] == ["container", "rename"]:
            state = self.find(args[-2])
            old = next(name for name, value in self.containers.items() if value is state)
            self.containers[args[-1]] = self.containers.pop(old)
        elif args[:2] == ["container", "start"]:
            self.find(args[-1])["State"]["Running"] = True
        elif args[:2] == ["container", "stop"]:
            self.find(args[-1])["State"]["Running"] = False
        elif args[:2] == ["container", "rm"]:
            state = self.find(args[-1])
            name = next(name for name, value in self.containers.items() if value is state)
            del self.containers[name]
        elif args[:2] == ["container", "wait"]:
            if self.wait_timeout:
                raise subprocess.TimeoutExpired(argv, timeout, stderr="sensitive")
            self.find(args[-1])["State"]["Running"] = False
            out = "0\n"
        elif args[:2] == ["container", "logs"]:
            phase = self.find(args[-1])["phase"]
            out = self.migration_override or "MIGRATE_RESULT " + json.dumps(
                {
                    "phase": phase,
                    "ok": self.migration_status == "ok",
                    "current": None if phase == "precheck" else "001_users",
                    "expected": "001_users",
                    "applied": ["001_users"] if phase == "up" else [],
                    "signature": digest("schema"),
                    "fingerprint": {
                        "version": "8.4",
                        "sql_mode": "",
                        "collation": "utf8mb4",
                        "time_zone": "UTC",
                        "ssl_version": "",
                    },
                }
            )
        elif args[:2] == ["volume", "inspect"]:
            out = json.dumps({"Driver": "local", "Options": self.volume_options})
        return subprocess.CompletedProcess(argv, 0, out, "")

    def find(self, identifier: str) -> dict:
        return next(
            value
            for name, value in self.containers.items()
            if name == identifier or value["Id"] == identifier
        )


@pytest.fixture
def runtime(tmp_path: Path):
    fake = FakeDocker()
    ctx = RunContext(
        "unit-run",
        project="unit-project",
        images={"was": fake.refs[0]},
        platform={
            "onprem": {
                "docker_host": "unix:///test.sock",
                "tiers": {
                    "was": {
                        "name": "unit-was",
                        "platform": "linux/arm64",
                        "ports": ["127.0.0.1:18080:8000"],
                        "volumes": [{"name": "unit-data", "target": "/data"}],
                        "env_file": str(tmp_path / "was.env"),
                    }
                },
            }
        },
    )
    provider = OnPremProvider(fake)
    provider.inject_config(["SECRET_KEY"], ctx)
    return fake, provider, ctx


def test_real_runner_uses_list_timeout_and_kills_descendants() -> None:
    result = subprocess_runner(
        [sys.executable, "-c", "import sys; sys.stdout.write('ok')"], timeout=2
    )
    assert result.returncode == 0 and result.stdout == "ok"
    started = time.monotonic()
    # 자식이 같은 stdout pipe를 상속한다. 부모만 죽이면 communicate()가 계속 대기한다.
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess_runner(
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
                "time.sleep(10)",
            ],
            timeout=0.1,
        )
    assert time.monotonic() - started < 2


def test_replace_rollback_and_repeated_rollback(runtime) -> None:
    fake, provider, ctx = runtime
    first = provider.deploy("was", ctx)
    assert first.observation.platform_digest == fake.children[0]
    assert first.observation.platform_digest != fake.images[fake.refs[0]]["Id"]
    second_ctx = replace(
        ctx,
        images={"was": fake.refs[1]},
        previous_release={"local": {"release_id": ctx.run_id, "images": {"was": fake.refs[0]}}},
    )
    mark = len(fake.calls)
    second = provider.deploy("was", second_ctx)
    calls = fake.calls[mark:]
    assert second.previous_image == first.image_ref
    assert next(i for i, a in enumerate(calls) if a[:2] == ["image", "pull"]) < next(
        i for i, a in enumerate(calls) if a[:2] == ["container", "stop"]
    )
    assert next(i for i, a in enumerate(calls) if a[:2] == ["container", "create"]) < next(
        i for i, a in enumerate(calls) if a[:2] == ["container", "stop"]
    )
    assert provider.rollback("was", second_ctx).image_ref == fake.refs[0]
    mark = len(fake.calls)
    assert provider.rollback("was", second_ctx).changed is False
    assert not any(
        a[:2] in (["container", "stop"], ["container", "rm"], ["container", "create"])
        for a in fake.calls[mark:]
    )


@pytest.mark.parametrize("prefix", [["image", "pull"], ["container", "create"]])
def test_prepare_failure_keeps_previous_running(runtime, prefix) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    fake.fail_prefix = prefix
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", replace(ctx, images={"was": fake.refs[1]}))
    assert "sensitive" not in str(error.value)
    assert fake.containers["unit-was"]["State"]["Running"]
    assert fake.containers["unit-was"]["Config"]["Image"] == fake.refs[0]


@pytest.mark.parametrize("label", ["ddak.managed", "ddak.project", "ddak.tier"])
def test_foreign_labels_protect_container(runtime, label) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    fake.containers["unit-was"]["Config"]["Labels"][label] = "foreign"
    mark = len(fake.calls)
    with pytest.raises(DdakToolError):
        provider.rollback("was", ctx)
    assert not any(a[:2] in (["container", "stop"], ["container", "rm"]) for a in fake.calls[mark:])


def test_first_release_rollback_removes_only_owned_and_is_idempotent(runtime) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    assert provider.rollback("was", ctx).changed
    assert provider.rollback("was", ctx).changed is False
    assert fake.containers == {}


def test_stopped_previous_release_restarts(runtime) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    fake.containers["unit-was"]["State"]["Running"] = False
    ctx = replace(
        ctx, previous_release={"local": {"release_id": ctx.run_id, "images": {"was": fake.refs[0]}}}
    )
    assert provider.rollback("was", ctx).changed
    assert fake.containers["unit-was"]["State"]["Running"]


@pytest.mark.parametrize(
    "extra",
    [
        {"privileged": True},
        {"cap_add": ["SYS_ADMIN"]},
        {"network": "host"},
        {"volumes": [{"name": "/host", "target": "/data"}]},
        {"binds": ["/host:/data"]},
        {"env": {"PASSWORD": "forbidden"}},
        {"ports": ["0.0.0.0:8080:80"]},
    ],
)
def test_unsafe_inventory_is_rejected_before_docker(runtime, extra) -> None:
    fake, provider, ctx = runtime
    platform = copy.deepcopy(ctx.platform)
    platform["onprem"]["tiers"]["was"].update(extra)
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", replace(ctx, platform=platform))
    assert error.value.code is ErrorCode.CONFIG_INVALID
    assert fake.calls == []


def test_named_volume_cannot_hide_bind_driver(runtime) -> None:
    fake, provider, ctx = runtime
    fake.volume_options = {"type": "none", "device": "/host", "o": "bind"}
    with pytest.raises(DdakToolError):
        provider.deploy("was", ctx)
    assert not fake.containers


def test_anonymous_image_volume_is_rejected(runtime) -> None:
    fake, provider, ctx = runtime
    fake.images[fake.refs[0]]["Volumes"] = {"/unlisted": {}}
    with pytest.raises(DdakToolError):
        provider.deploy("was", ctx)
    assert not fake.containers


@pytest.mark.parametrize("case", ["config", "platform", "raw"])
def test_manifest_and_config_corruption_fails_before_stop(runtime, case) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    mark = len(fake.calls)
    if case == "config":
        fake.images[fake.refs[1]]["Id"] = digest("unrelated")
    elif case == "platform":
        fake.images[fake.refs[1]]["Architecture"] = "amd64"
    else:
        fake.manifests[fake.refs[1]] += " "
    with pytest.raises(DdakToolError):
        provider.deploy("was", replace(ctx, images={"was": fake.refs[1]}))
    assert not any(a[:2] == ["container", "stop"] for a in fake.calls[mark:])


def test_deadline_stops_before_any_cli_and_bounds_each_command(runtime) -> None:
    fake, provider, ctx = runtime
    with pytest.raises(DdakToolError) as error:
        provider.deploy("was", replace(ctx, deadline=time.monotonic() - 1))
    assert error.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert fake.calls == []
    provider.deploy("was", replace(ctx, deadline=time.monotonic() + 2))
    assert all(timeout <= 2 for timeout in fake.timeouts)


def test_timeout_does_not_leak_process_output() -> None:
    def runner(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output="private", stderr="private")

    with pytest.raises(DdakToolError) as error:
        DockerHost(runner, endpoint="unix:///test.sock").run("info")
    assert error.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert "private" not in str(error.value)


def test_env_secret_reused_without_exposure_and_permission_fixed(runtime) -> None:
    _, provider, ctx = runtime
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    original = path.read_text()
    secret = original.strip().split("=", 1)[1]
    assert len(secret) == 64
    assert all(char in "0123456789abcdef" for char in secret)
    result = provider.inject_config(["SECRET_KEY"], ctx)
    assert result.changed is False
    assert result.keys == ["SECRET_KEY"]
    assert secret not in result.model_dump_json() and secret not in str(ctx)
    assert path.read_text() == original
    os.chmod(path, 0o644)
    assert provider.inject_config([], ctx).changed
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == original


def test_env_symlink_and_missing_requested_keys_rejected(runtime, tmp_path) -> None:
    _, provider, ctx = runtime
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    with pytest.raises(DdakToolError):
        provider.inject_config(["DB_PASSWORD"], ctx)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(DdakToolError):
        provider.inject_config([], ctx)
    assert not (tmp_path / "missing").exists()


def test_migration_three_phases_structured_and_cleaned(runtime) -> None:
    fake, provider, ctx = runtime
    result = provider.migrate_db(["001_users"], ctx)
    assert result.changed
    assert result.migration["event"] == "MIGRATE_RESULT"
    assert [r["phase"] for r in result.migration["phases"]] == ["precheck", "up", "verify"]
    creates = [a for a in fake.calls if a[:2] == ["container", "create"]]
    assert all("--entrypoint" in a and a[-1] == "--json" for a in creates)
    assert not fake.containers


@pytest.mark.parametrize("case", ["status", "json", "unverified", "timeout", "secret"])
def test_migration_failure_stops_and_cleans(runtime, case) -> None:
    fake, provider, ctx = runtime
    requested = ["001_users"]
    if case == "status":
        fake.migration_status = "failed"
    elif case == "json":
        fake.migration_override = "not json"
    elif case == "unverified":
        requested = ["missing"]
    elif case == "timeout":
        fake.wait_timeout = True
    else:
        fake.migration_override = json.dumps(
            {
                "phase": "precheck",
                "status": "ok",
                "changed": False,
                "migrations": [],
                "password": "private",
            }
        )
    with pytest.raises(DdakToolError) as error:
        provider.migrate_db(requested, ctx)
    assert "private" not in str(error.value)
    assert not fake.containers
    if case != "unverified":
        assert len([a for a in fake.calls if a[:2] == ["container", "create"]]) == 1


def test_health_remains_injected_and_old_result_defaults_work() -> None:
    result = ProviderResult(provider="custom", function="health_check")
    assert result.observation is None and result.passed
    assert OnPremProvider(health_checker=lambda ctx: result).health_check(RunContext("r")) == result
    with pytest.raises(DdakToolError, match="health 대상 이미지"):
        OnPremProvider().health_check(RunContext("r"))


def test_public_settings_upsert_and_preserve_runtime_secret(runtime) -> None:
    _, provider, ctx = runtime
    config = copy.deepcopy(ctx.platform)
    tier = config["onprem"]["tiers"]["was"]
    path = Path(tier["env_file"])
    initial = path.read_text()
    with path.open("a") as stream:
        stream.write("# retain operator setting\nDB_PASSWORD=" + "test-" + "only\nDB_HOST=old\n")
    tier["public_env"] = {
        "APP_BASE_URL": "http://localhost:8080",
        "DB_HOST": "demo-db",
        "DB_PORT": "3306",
        "DB_NAME": "flaskr",
        "SESSION_COOKIE_SECURE": "false",
    }
    updated = replace(ctx, platform=config)
    result = provider.inject_config(["DB_PASSWORD"], updated)
    assert result.changed
    contents = path.read_text()
    assert contents.startswith(initial)
    assert "DB_HOST=demo-db\n" in contents and "DB_HOST=old" not in contents
    assert "# retain operator setting" in contents
    assert "APP_BASE_URL=http://localhost:8080\n" in contents
    assert provider.inject_config([], updated).changed is False
    secret = initial.strip().split("=", 1)[1]
    assert secret not in result.model_dump_json()
    assert secret not in str(updated)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "values",
    [
        {"SECRET_KEY": "x" * 64},
        {"DB_PASSWORD": "private"},
        {"DATABASE_URL": "mysql://user:password@db/app"},
        {"APP_BASE_URL": "http://user:password@localhost:8080"},
        {"APP_BASE_URL": "http://localhost:8080/?token=private"},
        {"DB_HOST": "mysql://user:password@db"},
        {"DB_HOST": "db\nSECRET_KEY=bad"},
        {"DB_PORT": "65536"},
        {"SESSION_COOKIE_SECURE": "maybe"},
    ],
)
def test_credential_and_invalid_public_settings_rejected(runtime, values) -> None:
    fake, provider, ctx = runtime
    config = copy.deepcopy(ctx.platform)
    config["onprem"]["tiers"]["was"]["public_env"] = values
    with pytest.raises(DdakToolError) as error:
        provider.inject_config([], replace(ctx, platform=config))
    assert error.value.code is ErrorCode.CONFIG_INVALID
    assert "private" not in str(error.value)
    assert not fake.calls


@pytest.mark.parametrize("native", [True, False])
def test_classic_config_id_and_native_platform_descriptor_are_distinct(runtime, native) -> None:
    fake, provider, ctx = runtime
    fake.native = native
    if native:
        for ref, child in zip(fake.refs, fake.children, strict=True):
            fake.images[ref]["Id"] = child
            fake.images[ref]["Descriptor"] = {
                "digest": child,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
            }
    result = provider.deploy("was", ctx)
    assert result.observation.platform_digest == fake.children[0]
    assert provider.deploy("was", ctx).changed is False


def test_native_container_wrong_child_is_not_accepted(runtime) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    state = fake.containers["unit-was"]
    state["ImageManifestDescriptor"] = {
        "digest": fake.children[1],
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
    }
    observation, local = DockerHost(fake, endpoint="unix:///test.sock").image(
        fake.refs[0], "linux/arm64"
    )
    assert DockerHost.matches(state, local, observation) is False


def test_incomplete_previous_release_is_not_first_deploy_cleanup(runtime) -> None:
    fake, provider, ctx = runtime
    provider.deploy("was", ctx)
    mark = len(fake.calls)
    with pytest.raises(DdakToolError):
        provider.rollback("was", replace(ctx, previous_release={"local": {}}))
    assert not any(a[:2] in (["container", "stop"], ["container", "rm"]) for a in fake.calls[mark:])
