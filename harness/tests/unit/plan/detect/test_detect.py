"""plan/detect: 변경 탐지."""

from __future__ import annotations

from pathlib import Path

import pytest

from ddak.app import load_tools
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.deploy_request import DeployRequest
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.plan_facts import FileMeta
from ddak.core.contracts.release import SnapshotBinding
from ddak.core.contracts.tools.detect_changed_tiers import DetectChangedTiersInput
from ddak.core.snapshots import digest_json, file_manifest
from ddak.executor.service import source_facts
from ddak.plan.detect import detect_changed_tiers, facts_reader

CFG = {
    "tiers": {"web": {"paths": ["web"]}, "was": {"paths": ["was"]}},
    "migrations_dir": "db/migrations",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


V1 = {"web/index.html": "v1", "was/app.py": "v1", "db/migrations/0001_init.sql": "a"}


def _manifest(root: Path) -> dict[str, FileMeta]:
    return {k: FileMeta.model_validate(v) for k, v in file_manifest(root).items()}


def _run(root: Path, previous, cfg=CFG):
    h = digest_json(file_manifest(root))
    inp = DetectChangedTiersInput(
        run_id="run-1",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="both"),
        source_dir="src",
        snapshot=SnapshotBinding(source_snapshot_hash=h, build_snapshot_hash=h),
        previous=previous,
    )
    return detect_changed_tiers(inp, RunContext("run-1", deploy_config=cfg), root=root.parent)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    _write(root, V1)
    return root


def test_no_change(src: Path) -> None:
    out = _run(src, {"local": _manifest(src), "cloud": _manifest(src)})
    assert out.changed == {
        "local": {"web": False, "was": False},
        "cloud": {"web": False, "was": False},
    }
    assert out.new_migrations == () and out.changed_paths == ()


def test_v1_to_v2_scenario(src: Path) -> None:
    v1 = _manifest(src)
    _write(src, {"was/app.py": "v2", "db/migrations/0002_v2_users.sql": "b", "notes.txt": "x"})
    out = _run(src, {"local": v1})
    assert out.changed["local"] == {"web": False, "was": True}
    assert out.new_migrations == ("0002",)
    assert out.changed_paths == ("db/migrations/0002_v2_users.sql", "notes.txt", "was/app.py")


def test_web_only(src: Path) -> None:
    v1 = _manifest(src)
    _write(src, {"web/index.html": "v2"})
    assert _run(src, {"local": v1}).changed["local"] == {"web": True, "was": False}


def test_deleted_file(src: Path) -> None:
    v1 = _manifest(src)
    (src / "web/index.html").unlink()
    out = _run(src, {"local": v1})
    assert out.changed["local"]["web"] is True
    assert out.changed_paths == ("web/index.html",)


def test_executable_only(src: Path) -> None:
    v1 = _manifest(src)
    (src / "was/app.py").chmod(0o755)
    assert _run(src, {"local": v1}).changed["local"] == {"web": False, "was": True}


def test_bootstrap_and_local_only(src: Path) -> None:
    out = _run(src, {"local": None, "cloud": None})
    assert out.changed["cloud"] == {"web": True, "was": True}
    assert out.new_migrations == ("0001",)
    assert out.changed_paths == tuple(sorted(V1))
    v1 = _manifest(src)
    out = _run(src, {"local": v1, "cloud": None})
    assert out.changed["local"] == {"web": False, "was": False}
    assert out.changed["cloud"] == {"web": True, "was": True}
    assert out.new_migrations == ("0001",)  # cloud에는 아직 없음


def test_env_file_ignored(src: Path) -> None:
    v1 = _manifest(src)
    _write(src, {".env": "A=1", "web/.env.local": "B=2"})
    out = _run(src, {"local": v1})
    assert out.changed["local"] == {"web": False, "was": False}


def test_migration_without_prefix_ignored(src: Path) -> None:
    v1 = _manifest(src)
    _write(src, {"db/migrations/README.md": "x"})
    assert _run(src, {"local": v1}).new_migrations == ()


def test_facts_hash_matches_source_facts(src: Path) -> None:
    out = _run(src, {"local": None})
    assert out.facts_hash == source_facts(src) == facts_reader(src)


def test_source_changed_after_intake(src: Path) -> None:
    h = digest_json(file_manifest(src))
    _write(src, {"was/app.py": "tampered"})
    inp = DetectChangedTiersInput(
        run_id="run-1",
        request=DeployRequest(project="demo", repo_url="https://github.com/o/r", target="both"),
        source_dir="src",
        snapshot=SnapshotBinding(source_snapshot_hash=h, build_snapshot_hash=h),
        previous={"local": None},
    )
    with pytest.raises(DdakToolError) as e:
        detect_changed_tiers(inp, RunContext("run-1", deploy_config=CFG), root=src.parent)
    assert e.value.code is ErrorCode.PRECONDITION_FAILED
    assert str(src.parent) not in str(e.value)


def test_registered() -> None:
    assert "detect_changed_tiers" in load_tools().registered()
