"""새 앱 환경 계약: migration 전용 URL 별칭과 파이프라인 버전 키."""

from dataclasses import replace
from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError
from tests.unit.onprem.deploy import test_onprem as support

runtime = support.runtime


def test_migration_alias_is_only_in_private_migration_env(runtime, tmp_path):
    fake, provider, ctx = runtime
    tier = ctx.platform["onprem"]["tiers"]["was"]
    app_env = Path(tier["env_file"])
    before = app_env.read_bytes()
    migration = tmp_path / "migration.env"
    url = "mysql+pymysql://migrator:" + "unit-fixture" + "@db/app"
    migration.write_text("DATABASE_URL_MIGRATOR=" + url + "\n")
    migration.chmod(0o600)
    tier["migration_env_file"] = str(migration)
    provider.migrate_db(["001_users"], ctx)
    env = dict(line.split("=", 1) for line in migration.read_text().splitlines())
    assert env == {"DATABASE_URL": url, "DATABASE_URL_MIGRATOR": url}
    assert app_env.read_bytes() == before
    creates = [args for args in fake.calls if args[:2] == ["container", "create"]]
    assert len(creates) == 3
    assert all(args[args.index("--env-file") + 1] == str(migration) for args in creates)
    assert all(url not in " ".join(args) for args in fake.calls)


@pytest.mark.parametrize("same", [True, False])
def test_migration_env_must_exist_and_be_separate(runtime, tmp_path, same):
    fake, provider, ctx = runtime
    tier = ctx.platform["onprem"]["tiers"]["was"]
    path = Path(tier["env_file"]) if same else tmp_path / "absent.env"
    tier["migration_env_file"] = str(path)
    with pytest.raises(DdakToolError):
        provider.migrate_db(["001_users"], ctx)
    assert not any(a[:2] == ["container", "create"] for a in fake.calls)
    if not same:
        assert not path.exists()


def test_pipeline_version_keys_do_not_require_host_env(runtime):
    fake, provider, ctx = runtime
    ctx = replace(ctx, source_sha="a" * 40, candidate_sha="b" * 40)
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    before = path.read_bytes()
    provider.inject_config(["RELEASE_ID", "SOURCE_SHA"], ctx)
    assert path.read_bytes() == before
    provider.deploy("was", ctx)
    create = next(a for a in fake.calls if a[:2] == ["container", "create"])
    assert "RELEASE_ID=" + ctx.run_id in create
    assert "SOURCE_SHA=" + "b" * 40 in create


def test_runtime_rejects_migration_account_key(runtime):
    fake, provider, ctx = runtime
    path = Path(ctx.platform["onprem"]["tiers"]["was"]["env_file"])
    with path.open("a") as out:
        out.write("DATABASE_URL_MIGRATOR=fixture\n")
    with pytest.raises(DdakToolError, match="마이그레이션"):
        provider.deploy("was", ctx)
    assert not any(a[:2] == ["container", "create"] for a in fake.calls)


def test_missing_candidate_is_not_reported_as_user_env_key(runtime):
    _, provider, ctx = runtime
    with pytest.raises(DdakToolError, match="후보 커밋"):
        provider.inject_config(["SOURCE_SHA"], ctx)


def test_rollback_supplies_previous_candidate_sha(runtime):
    fake, provider, ctx = runtime
    provider.deploy("was", replace(ctx, source_sha="a" * 40, candidate_sha="a" * 40))
    v2 = replace(
        ctx,
        run_id="unit-v2",
        source_sha="b" * 40,
        candidate_sha="b" * 40,
        images={"was": fake.refs[1]},
        previous_release={
            "local": {
                "release_id": ctx.run_id,
                "candidate_sha": "a" * 40,
                "images": ctx.images,
            }
        },
    )
    provider.deploy("was", v2)
    provider.rollback("was", v2)
    last = [a for a in fake.calls if a[:2] == ["container", "create"]][-1]
    assert "SOURCE_SHA=" + "a" * 40 in last
    assert "RELEASE_ID=" + ctx.run_id in last


@pytest.mark.parametrize("body", ["", "DATABASE_URL_MIGRATOR=\n", "DATABASE_URL=\n"])
def test_empty_migration_url_rejected_before_create(runtime, tmp_path, body):
    fake, provider, ctx = runtime
    path = tmp_path / "migrate.env"
    path.write_text(body)
    path.chmod(0o600)
    ctx.platform["onprem"]["tiers"]["was"]["migration_env_file"] = str(path)
    with pytest.raises(DdakToolError, match="DB URL"):
        provider.migrate_db(["001_users"], ctx)
    assert not any(a[:2] == ["container", "create"] for a in fake.calls)


def test_sqlite_migration_url_without_migrator_key_remains_supported(runtime, tmp_path):
    _, provider, ctx = runtime
    path = tmp_path / "migrate.env"
    path.write_text("DATABASE_URL=sqlite:////data/app.sqlite\n")
    path.chmod(0o600)
    ctx.platform["onprem"]["tiers"]["was"]["migration_env_file"] = str(path)
    provider.migrate_db(["001_users"], ctx)
    assert path.read_text() == "DATABASE_URL=sqlite:////data/app.sqlite\n"
