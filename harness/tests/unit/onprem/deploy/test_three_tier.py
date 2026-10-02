"""source=fake: tier별 접속과 DB 보존. 실제 SSH·Docker 프로세스는 실행하지 않는다."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import RunMode
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.store import Store
from ddak.onprem.deploy import OnPremProvider, preflight_inventory, reset_demo
from tests.unit.onprem.deploy.test_onprem import FakeDocker
from tests.unit.onprem.deploy.test_onprem_vm import PIN, FakeVM
from tests.unit.onprem.deploy.test_onprem_vm import vm as vm


class DatabaseDocker(FakeDocker):
    """기존 fake에 named volume 존재와 실제 --mount 관측만 추가한다."""

    def __init__(self):
        super().__init__()
        self.volumes = set()
        self.mysql_refs = [ref.replace("example/app@", "mysql@") for ref in self.refs]
        for ref, manifest in list(self.manifests.items()):
            self.manifests[ref.replace("example/app@", "mysql@")] = manifest
        for old, new in zip(self.refs, self.mysql_refs, strict=True):
            self.images[new] = {**self.images[old], "Volumes": {"/var/lib/mysql": {}}}

    def __call__(self, argv, *, timeout):
        result = super().__call__(argv, timeout=timeout)
        if result.returncode:
            return result
        args = argv[3:]
        if args[:2] == ["volume", "ls"]:
            pattern = args[-1].removeprefix("name=")
            result.stdout = "\n".join(v for v in self.volumes if re.fullmatch(pattern, v))
        elif args[:2] == ["volume", "create"]:
            self.volumes.add(args[-1])
        elif args[:2] == ["volume", "rm"]:
            self.volumes.discard(args[-1])
        elif args[:2] == ["container", "create"]:
            state = self.containers[args[args.index("--name") + 1]]
            state["Mounts"] = []
            for index, arg in enumerate(args):
                if arg == "--mount":
                    mount = dict(part.partition("=")[::2] for part in args[index + 1].split(","))
                    state["Mounts"].append(
                        {
                            "Type": mount["type"],
                            "Name": mount["src"],
                            "Destination": mount["dst"],
                            "RW": "readonly" not in mount,
                        }
                    )
        elif args[:2] == ["container", "inspect"]:
            # fake도 요청하지 않은 필드를 돌려주지 않아 Mounts 누락 회귀를 잡는다.
            state = json.loads(result.stdout)
            if "Mounts" not in args[args.index("--format") + 1]:
                state.pop("Mounts", None)
            result.stdout = json.dumps(state)
        return result


def mutations(calls):
    return [
        call
        for call in calls
        if call[:2]
        in (
            ["image", "pull"],
            ["volume", "create"],
            ["volume", "rm"],
            ["container", "create"],
            ["container", "cp"],
            ["container", "start"],
            ["container", "stop"],
            ["container", "rm"],
            ["container", "rename"],
        )
    ]


@pytest.fixture(autouse=True)
def no_external_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("이 테스트는 외부 프로세스를 실행하면 안 된다")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def database(tmp_path):
    fake = DatabaseDocker()
    source = tmp_path / "approved-source"
    assets = source / "docker" / "mysql"
    assets.mkdir(parents=True)
    for name in ("ddak.cnf", "10-init.sh", "init.sql.template", "ready.sh"):
        (assets / name).write_text("# source=fake\n")
    env = tmp_path / "db.env"
    env.write_text("MYSQL_PASSWORD=" + "fixture-" + "only\n")
    env.chmod(0o600)
    inventory = {
        "docker_host": "unix:///test.sock",
        "tiers": {
            "db": {
                "name": "unit-db",
                "kind": "mysql",
                "platform": "linux/arm64",
                "network": "db-net",
                "env_file": str(env),
                "volumes": [{"name": "unit-db-data", "target": "/var/lib/mysql"}],
            }
        },
    }
    ctx = RunContext(
        "rel-v1",
        mode=RunMode.BOOTSTRAP,
        project="flaskr",
        images={"db": fake.mysql_refs[0]},
        build_source=str(source),
        platform={"onprem": inventory},
    )
    return fake, OnPremProvider(fake), ctx


def test_new_database_copies_approved_assets_before_start(database):
    fake, provider, ctx = database
    result = provider.deploy("db", ctx)
    state = fake.containers["unit-db"]
    assert result.changed and result.observation.platform_digest == fake.children[0]
    assert fake.volumes == {"unit-db-data"}
    assert state["Mounts"] == [
        {"Type": "volume", "Name": "unit-db-data", "Destination": "/var/lib/mysql", "RW": True}
    ]
    copies = [c for c in fake.calls if c[:2] == ["container", "cp"]]
    destinations = {
        "ddak.cnf": "/etc/mysql/conf.d/ddak.cnf",
        "10-init.sh": "/docker-entrypoint-initdb.d/10-ddak-init.sh",
        "init.sql.template": "/opt/ddak-init.sql.template",
        "ready.sh": "/usr/local/bin/ddak-db-ready",
    }
    assert len(copies) == len(destinations)
    for call in copies:
        path = Path(call[2])
        assert path.parent == Path(ctx.build_source) / "docker/mysql"
        assert call[3] == state["Id"] + ":" + destinations[path.name]
    create = next(c for c in fake.calls if c[:2] == ["container", "create"])
    start = next(c for c in fake.calls if c[:2] == ["container", "start"])
    assert all(
        fake.calls.index(create) < fake.calls.index(c) < fake.calls.index(start) for c in copies
    )
    assert "python" not in create[create.index("--health-cmd") + 1]
    assert not any(c[:2] == ["container", "exec"] for c in fake.calls)


def test_existing_database_is_read_only_even_without_bootstrap_source(database):
    fake, provider, ctx = database
    provider.deploy("db", ctx)
    before = copy.deepcopy(fake.containers)
    env = Path(ctx.platform["onprem"]["tiers"]["db"]["env_file"])
    original = env.read_bytes()
    fake.calls.clear()
    result = provider.deploy("db", replace(ctx, run_id="rel-v2", build_source=None))
    assert not result.changed
    assert fake.containers == before and fake.volumes == {"unit-db-data"}
    assert env.read_bytes() == original
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("operation", ["cp", "start"])
def test_failed_database_bootstrap_and_rollback_keep_created_resources(database, operation):
    fake, provider, ctx = database
    fake.fail_prefix = ["container", operation]
    with pytest.raises(DdakToolError):
        provider.deploy("db", ctx)
    assert "unit-db" in fake.containers and fake.volumes == {"unit-db-data"}
    before = copy.deepcopy(fake.containers)
    fake.calls.clear()
    assert not provider.rollback("db", ctx).changed
    assert fake.containers == before and fake.volumes == {"unit-db-data"}
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("invalid", ["stopped", "mount", "readonly", "image", "foreign"])
def test_existing_database_conflicts_never_repair_or_recreate(database, invalid):
    fake, provider, ctx = database
    provider.deploy("db", ctx)
    state = fake.containers["unit-db"]
    if invalid == "stopped":
        state["State"]["Running"] = False
    elif invalid == "mount":
        state["Mounts"][0]["Name"] = "someone-elses-data"
    elif invalid == "readonly":
        state["Mounts"][0]["RW"] = False
    elif invalid == "image":
        ctx = replace(ctx, images={"db": fake.mysql_refs[1]})
    else:
        state["Config"]["Labels"]["ddak.project"] = "another-project"
    before = copy.deepcopy(fake.containers)
    fake.calls.clear()
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("db", ctx)
    assert caught.value.code == ErrorCode.PRECONDITION_FAILED
    assert fake.containers == before and fake.volumes == {"unit-db-data"}
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("condition", ["orphan", "lookup-failed"])
def test_no_database_container_does_not_prove_a_fresh_database(database, condition):
    fake, provider, ctx = database
    if condition == "orphan":
        fake.volumes.add("unit-db-data")
    else:
        fake.fail_prefix = ["volume", "ls"]
    before = set(fake.volumes)
    with pytest.raises(DdakToolError):
        provider.deploy("db", ctx)
    assert fake.containers == {} and fake.volumes == before
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("has_previous", [False, True])
def test_database_rollback_preserves_container_and_volume(database, has_previous):
    fake, provider, ctx = database
    provider.deploy("db", ctx)
    before = copy.deepcopy(fake.containers)
    if has_previous:
        ctx = replace(
            ctx,
            previous_release={
                "local": {
                    "release_id": "older",
                    "images": {"db": fake.mysql_refs[1]},
                }
            },
        )
    fake.calls.clear()
    assert not provider.rollback("db", ctx).changed
    assert fake.containers == before and fake.volumes == {"unit-db-data"}
    assert mutations(fake.calls) == []


def test_reset_rejects_database_release_before_runtime_or_secret_changes(
    database, tmp_path, monkeypatch
):
    fake, provider, ctx = database
    provider.deploy("db", ctx)
    state = tmp_path / "state"
    store = Store(state / "ddak.sqlite")
    release = {
        "release_id": ctx.run_id,
        "source_mode": "real",
        "source": {},
        "files": {},
        "source_files": {},
        "images": dict(ctx.images),
        "artifacts": None,
    }
    result = {"status": "SUCCEEDED", "tracks": {"local": "DONE"}}
    store.create_run(ctx.run_id, ctx.project, "sha256:" + "a" * 64)
    store.finish(
        ctx.run_id,
        "SUCCEEDED",
        result,
        {**release, "result": result},
        {"local": ("SUCCEEDED", release)},
    )
    monkeypatch.setenv("ALLOW_DEMO_RESET", "1")
    env = Path(ctx.platform["onprem"]["tiers"]["db"]["env_file"])
    env.write_text(env.read_text() + "SECRET_KEY=" + "a" * 64 + "\n")
    original = env.read_bytes()
    before = copy.deepcopy(fake.containers)
    ledger = store.environments(ctx.project)
    fake.calls.clear()
    with pytest.raises(DdakToolError, match=r"DB|db"):
        reset_demo(state, ctx.project, ctx.platform["onprem"], ctx.run_id, runner=fake)
    assert fake.containers == before and fake.volumes == {"unit-db-data"}
    assert env.read_bytes() == original and store.environments(ctx.project) == ledger
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("change", ["release", "image", "config"])
def test_web_only_replaces_when_image_or_config_changes(change):
    fake = FakeDocker()
    provider = OnPremProvider(fake)
    ctx = RunContext(
        "web-v1",
        project="flaskr",
        images={"web": fake.refs[0]},
        platform={
            "onprem": {
                "docker_host": "unix:///test.sock",
                "tiers": {
                    "web": {
                        "name": "unit-web",
                        "kind": "nginx",
                        "platform": "linux/arm64",
                    }
                },
            },
        },
    )
    provider.deploy("web", ctx)
    before = copy.deepcopy(fake.containers)
    updated = replace(ctx, run_id="web-v2", platform=copy.deepcopy(ctx.platform))
    if change == "image":
        updated = replace(updated, images={"web": fake.refs[1]})
    elif change == "config":
        updated.platform["onprem"]["tiers"]["web"]["ready"] = {"path": "/health/other"}
    fake.calls.clear()
    result = provider.deploy("web", updated)
    assert result.changed is (change != "release")
    if change == "release":
        assert fake.containers == before
        assert not any(c[0] == "container" for c in mutations(fake.calls))
    else:
        assert fake.containers["unit-web"]["Id"] != before["unit-web"]["Id"]


@pytest.fixture
def three_vm_inventory(tmp_path, monkeypatch):
    import ddak.onprem.deploy.ssh as ssh

    monkeypatch.setattr(ssh.shutil, "which", lambda name: "/usr/bin/ssh")
    key = tmp_path / "fake-identity"
    key.touch(mode=0o600)
    connection = {"user": "deploy", "key_path": str(key), "host_key_fingerprint": PIN}
    return {
        "mode": "vm",
        "ssh": {**connection, "host": "192.0.2.2"},
        "tiers": {
            "web": {
                "name": "web",
                "kind": "nginx",
                "platform": "linux/arm64",
                "network": "web-net",
                "ssh": {**connection, "host": "192.0.2.4"},
            },
            "was": {"name": "was", "platform": "linux/arm64", "network": "was-net"},
            "db": {
                "name": "db",
                "kind": "mysql",
                "platform": "linux/arm64",
                "network": "db-net",
                "ssh": {**connection, "host": "192.0.2.3"},
                "volumes": [{"name": "db-data", "target": "/var/lib/mysql"}],
            },
        },
    }


def test_each_tier_dispatches_to_its_ssh_host_with_legacy_fallback(three_vm_inventory):
    fake = FakeVM()
    provider = OnPremProvider(fake)
    ctx = RunContext("routing", project="flaskr", platform={"onprem": three_vm_inventory})
    for tier, host in (("db", "192.0.2.3"), ("was", "192.0.2.2"), ("web", "192.0.2.4")):
        with provider._session(tier, ctx) as (docker, _):
            docker.run("version")
        assert f"HostName {host}\n" in fake.config_texts[-1]
    assert [a[-1] for a in fake.argvs if a[0] == "ssh-keyscan"] == [
        "192.0.2.3",
        "192.0.2.2",
        "192.0.2.4",
    ]
    assert len([c for c in fake.calls if c[0] == "version"]) == 3
    assert all(not p.exists() for p in fake.tempdirs)


@pytest.mark.parametrize("bad_host", [None, "192.0.2.3"])
def test_preflight_checks_each_host_without_mutation(three_vm_inventory, bad_host):
    fake = FakeVM()
    checked = []

    def runner(argv, *, timeout):
        result = fake(argv, timeout=timeout)
        if "version" in argv:
            host = re.search(r"^    HostName (.+)$", fake.config_texts[-1], re.MULTILINE)[1]
            checked.append(host)
            result.stdout = json.dumps(
                {
                    "Server": {
                        "ApiVersion": "1.48" if host == bad_host else "1.49",
                        "Os": "linux",
                        "Arch": "arm64",
                    },
                    "Client": {"ApiVersion": "1.49"},
                }
            )
        elif "network" in argv:
            host = re.search(r"^    HostName (.+)$", fake.config_texts[-1], re.MULTILINE)[1]
            assert (
                argv[-1]
                == {
                    "192.0.2.4": "web-net",
                    "192.0.2.2": "was-net",
                    "192.0.2.3": "db-net",
                }[host]
            )
            result.stdout = json.dumps(argv[-1])
        return result

    report = preflight_inventory(three_vm_inventory, runner=runner)
    assert report["passed"] is (bad_host is None)
    assert set(checked) == {"192.0.2.4", "192.0.2.2", "192.0.2.3"}
    assert mutations(fake.calls) == []


@pytest.mark.parametrize("volume_remains", [False, True])
def test_update_never_creates_a_missing_database(database, volume_remains):
    fake, provider, ctx = database
    if volume_remains:
        fake.volumes.add("unit-db-data")
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("db", replace(ctx, mode=RunMode.UPDATE))
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert not fake.containers and not mutations(fake.calls)


def test_deploy_does_not_stop_the_last_healthy_replica():
    fake = DatabaseDocker()
    ctx = RunContext(
        "v1",
        project="flaskr",
        images={"was": fake.refs[0]},
        platform={
            "onprem": {
                "docker_host": "unix:///test.sock",
                "tiers": {
                    "was": {"name": "app", "platform": "linux/arm64", "replicas": 3, "ready": {}}
                },
            }
        },
    )
    provider = OnPremProvider(fake)
    provider.deploy("was", ctx)
    fake.containers["app-2"]["State"]["Running"] = False
    fake.containers["app-3"]["State"]["Health"]["Status"] = "unhealthy"
    before = copy.deepcopy(fake.containers)
    fake.calls.clear()
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("was", replace(ctx, run_id="v2", images={"was": fake.refs[1]}))
    assert caught.value.code is ErrorCode.PRECONDITION_FAILED
    assert fake.containers == before
    assert not [c for c in mutations(fake.calls) if c[:2] != ["image", "pull"]]


def test_bootstrap_cannot_recreate_database_recorded_in_previous_release(database):
    fake, provider, ctx = database
    ctx = replace(ctx, previous_release={"local": {"images": {"db": fake.mysql_refs[0]}}})
    with pytest.raises(DdakToolError):
        provider.deploy("db", ctx)
    assert not mutations(fake.calls)


def test_rollback_recovers_failed_peers_before_replacing_last_healthy(vm):
    fake, provider, ctx = vm
    ctx.platform["onprem"]["tiers"]["was"]["replicas"] = 3
    provider.deploy("was", ctx)
    fake.containers["app-3"]["State"]["Running"] = False
    update = replace(
        ctx,
        run_id="v2",
        images={"was": fake.refs[1]},
        previous_release={"local": {"release_id": ctx.run_id, "images": {"was": fake.refs[0]}}},
    )
    fake.fail_ready = "app-2"
    with pytest.raises(DdakToolError):
        provider.deploy("was", update)
    healthy_counts = []

    def observe(argv, *, timeout):
        result = fake(argv, timeout=timeout)
        healthy_counts.append(
            sum(
                bool(
                    s["State"]["Running"]
                    and (s["State"].get("Health") or {}).get("Status") == "healthy"
                )
                for name, s in fake.containers.items()
                if name in {"app-1", "app-2", "app-3"}
            )
        )
        return result

    provider.runner = observe
    provider.rollback("was", update)
    assert min(healthy_counts) >= 1
    assert all(
        s["Config"]["Image"] == fake.refs[0] and s["State"]["Running"]
        for s in fake.containers.values()
    )


@pytest.mark.parametrize(
    "filename,mode", [("10-init.sh", 0o755), ("10-init.sh", 0o600), ("ddak.cnf", 0o666)]
)
def test_database_asset_modes_fail_before_initialization(database, filename, mode):
    fake, provider, ctx = database
    (Path(ctx.build_source) / "docker" / "mysql" / filename).chmod(mode)
    with pytest.raises(DdakToolError) as caught:
        provider.deploy("db", ctx)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert not mutations(fake.calls) and not fake.containers and not fake.volumes
