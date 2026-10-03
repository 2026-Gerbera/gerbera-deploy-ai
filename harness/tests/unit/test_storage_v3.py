"""source=fixture: 저장 경로 탐지·패치·인프라 의도·런타임 주입·업로드 검증."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ddak.cloud.deploy.entry import RUNTIME_ENV_KEYS, _runtime_environment, put_secret_values
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.infra_outputs import checked_cloud_outputs, checked_outputs
from ddak.core.contracts.tools.smoke_test import SmokeTestInput
from ddak.core.patch_patterns import scan_patch_targets
from ddak.core.runtime_values import local_missing
from ddak.core.storage import bucket_name, scan_storage, storage_intent
from ddak.onprem.deploy.config import inject_config
from ddak.onprem.deploy.provider import OnPremProvider, _Tier
from ddak.plan.patch.check import check_patch
from ddak.plan.patch.intents import EditIntent, render_intents
from ddak.verify.smoke.fake import FakeFlaskr, FakeSmokeAdapter
from ddak.verify.smoke.logic import UPLOAD_PNG, ImageUpload, Response, UrlClient, run_smoke
from tests.support import load_script
from tests.unit import test_deployment_service as service_support
from tests.unit.plan import test_flow as flow_support
from tests.unit.plan.patch.test_generate_intents_fix11 import CFG, FakeProvider, propose_intents

CODE = """import os
from pathlib import Path
IMG_DIR = "img"
def store(image, name):
    directory = Path(IMG_DIR)
    image.save(directory / name)
"""
BUCKET = bucket_name("flaskr", 1)


def test_storage_evidence_is_ast_locations_without_values():
    found = scan_storage({"flaskr/uploads.py": CODE, "notes.txt": 'IMG_DIR = "img"'})
    assert [asdict(e) for e in found] == [
        {"file": "flaskr/uploads.py", "line": 3, "kind": "hardcoded_dir"},
        {"file": "flaskr/uploads.py", "line": 6, "kind": "file_write"},
    ]
    assert '"img"' not in json.dumps([asdict(e) for e in found])
    patched = CODE.replace('"img"', 'os.environ["IMG_DIR"]')
    assert {e.kind for e in scan_storage({"flaskr/uploads.py": patched})} == {
        "env_read",
        "file_write",
    }
    assert not scan_storage({"x.py": '# IMG_DIR = "img"\ntext="IMG_DIR"\n'})


@pytest.mark.parametrize(
    "needs,present,expected",
    [(True, False, "create"), (True, True, None), (False, True, "remove"), (False, False, None)],
)
def test_storage_intent(needs, present, expected):
    assert storage_intent(needs, present) == expected


@pytest.mark.parametrize(
    "cloud,target,source,intent",
    [
        ({}, "both", CODE, "create"),
        ({"upload_bucket": BUCKET}, "both", CODE, None),
        ({"upload_bucket": BUCKET}, "both", "version = 1\n", "remove"),
        ({}, "local", CODE, None),
    ],
)
def test_plan_flow_carries_storage_intent_and_smoke(tmp_path, cloud, target, source, intent):
    repo = tmp_path / "repo"
    files = {
        "deploy.yaml": "tiers:\n  was:\n    paths: [flaskr]\n    dockerfile: Dockerfile\n",
        "Dockerfile": "FROM fixture\n",
        "flaskr/uploads.py": source,
    }
    flow_support.commit(repo, files)
    bundle = flow_support.go(
        tmp_path,
        repo,
        request=flow_support.request(repo, target=target),
        platform={"cloud": cloud},
        source_context=RunContext(
            "run-1",
            project_settings={
                "cloud_platform": "flaskr",
                "aws_expected_account_id": "123456789012",
            },
        ),
    )
    meta = bundle.context.project_settings.get("_infra_storage")
    assert (meta["intent"] if meta else None) == intent
    if intent:
        assert meta["bucket"] == (BUCKET if intent == "remove" else None)
        assert bundle.facts.infra_inputs_changed
        assert any(s.tool == "apply_infra" for s in bundle.plan.deploy.cloud.steps)
    if source == CODE:
        assert "storage" in bundle.facts.smoke_groups
        assert next(k for k in bundle.facts.env_keys if k.name == "IMG_DIR").kind == "plain"
        for track in (bundle.plan.deploy.local, bundle.plan.deploy.cloud):
            for step in track.steps:
                if step.tool == "smoke_test":
                    assert step.params["scenarios"] == ["base", "storage"]
    else:
        assert "storage" not in bundle.facts.smoke_groups


def test_patch_intents_renderer_and_checker(tmp_path):
    source = tmp_path / "source"
    (source / "flaskr").mkdir(parents=True)
    (source / "flaskr/uploads.py").write_text(CODE)
    targets = scan_patch_targets(source, ["flaskr/uploads.py"])
    assert len(targets) == 1 and targets[0].pattern_id == "local_storage_dir"
    reply = {
        "intents": [
            {"file": t.file, "line": t.line, "pattern_id": t.pattern_id, "key": "IMG_DIR"}
            for t in targets
        ],
        "reason": "저장 경로 전환",
    }
    provider = FakeProvider(json.dumps(reply))
    patch, keys, _ = propose_intents(
        source,
        targets,
        RunContext("run-storage", toggles={"code_patch": True}),
        settings=CFG,
        provider=provider,
    )
    assert b"+IMG_DIR = os.environ['IMG_DIR']" in patch
    assert b'-IMG_DIR = "img"' in patch
    checked = check_patch(source, patch)
    assert checked.passed, checked.violations
    assert [(k.name, k.kind) for k in keys] == [("IMG_DIR", "plain")]
    assert "image.save" not in provider.seen[0].user
    with pytest.raises(ValueError):
        render_intents(
            source,
            targets,
            [EditIntent(file=targets[0].file, line=3, pattern_id="local_storage_dir", key="OTHER")],
        )


def test_upload_bucket_checked_and_rehydrated():
    assert checked_outputs({"upload_bucket": BUCKET}, "app") == {"upload_bucket": BUCKET}
    assert checked_cloud_outputs({"upload_bucket": BUCKET}) == {"upload_bucket": BUCKET}
    for value in ["https://example", "UPPER", "bad/bucket", "ddak-flaskr-uploads-not-account"]:
        with pytest.raises(ValueError):
            checked_outputs({"upload_bucket": value}, "app")


def test_cloud_public_storage_env_never_becomes_secret():
    ctx = RunContext(
        "run-storage", cloud_domain="app.example", platform={"cloud": {"upload_bucket": BUCKET}}
    )
    assert _runtime_environment(ctx, ctx.run_id, None)["was"]["IMG_DIR"] == f"s3://{BUCKET}/img"
    assert "IMG_DIR" in RUNTIME_ENV_KEYS
    result = put_secret_values(["IMG_DIR"], ctx)
    assert not result.changed
    assert (
        "IMG_DIR"
        not in _runtime_environment(
            RunContext("run-old", cloud_domain="app.example"), "run-old", None
        )["was"]
    )


def test_local_img_dir_supplied_without_operator_value(tmp_path):
    path = tmp_path / "runtime-values"
    config = _Tier(name="was", platform="linux/amd64", env_file=str(path))
    host = SimpleNamespace(check_deadline=lambda: None)
    provider = SimpleNamespace(name="onprem", _runtime=lambda *a: (host, config))
    assert local_missing({"tiers": {"was": {}}}, ["IMG_DIR"]) == []
    inject_config(provider, ["IMG_DIR"], RunContext("run-storage"))
    assert path.read_text() == "IMG_DIR=img\n"
    argv = OnPremProvider._args(config, "demo", "was")
    assert "IMG_DIR=img" in argv


@pytest.mark.parametrize("layout", ["was", "three"])
def test_three_replica_template_shares_upload_volume(tmp_path, layout):
    script = load_script("o1_onprem")
    # 초기화는 임시 fixture에만 쓰며 Docker·SSH는 실행하지 않는다.
    summary = script.initialize(tmp_path / layout, layout, "https://fixture.example", "linux/amd64")
    inventory = json.loads((tmp_path / layout / "inventory.json").read_text())
    was = inventory["tiers"]["was"]
    assert was["replicas"] == 3
    assert {"name": summary["project"] + "-uploads", "target": "/app/img"} in was["volumes"]


@pytest.mark.parametrize("target", ["local", "cloud"])
@pytest.mark.parametrize("broken", [None, "missing", "corrupt"])
def test_upload_and_six_get_hashes(target, broken):
    class Client(FakeFlaskr):
        reads = 0

        def request(self, method, path, form, timeout):
            response = super().request(method, path, form, timeout)
            if path.startswith("/uploads/"):
                self.reads += 1
                if broken == "missing" and self.reads == 4:
                    return Response(404, (), "")
                if broken == "corrupt" and self.reads == 4:
                    return Response(200, (), "", b"wrong-image")
            return response

    client = Client("run-storage")
    adapter = FakeSmokeAdapter(target)
    adapter.client = lambda _: client
    out = run_smoke(
        adapter,
        SmokeTestInput(run_id="run-storage", target=target, scenarios=["storage"]),
        RunContext("run-storage"),
    )
    assert out.passed is (broken is None)
    assert client.reads == 6
    assert out.scenarios[0].normalized["matched"] == (5 if broken else 6)
    assert out.scenarios[0].normalized["sha256"] == hashlib.sha256(UPLOAD_PNG).hexdigest()


def test_multipart_bytes_and_binary_response_with_fake_http(monkeypatch):
    assert UPLOAD_PNG.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks = []
    while offset < len(UPLOAD_PNG):
        size = struct.unpack(">I", UPLOAD_PNG[offset : offset + 4])[0]
        chunk = UPLOAD_PNG[offset + 4 : offset + 8 + size]
        crc = struct.unpack(">I", UPLOAD_PNG[offset + 8 + size : offset + 12 + size])[0]
        assert zlib.crc32(chunk) == crc
        chunks.append(chunk[:4])
        offset += size + 12
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    sent = []

    class Connection:
        sock = None

        def __init__(self, *a, **k):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def request(self, *a, **k):
            sent.append((a, k))

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                read=lambda n: UPLOAD_PNG,
                close=lambda: None,
                getheaders=lambda: [("Content-Type", "image/png")],
            )

    monkeypatch.setattr("ddak.verify.smoke.logic.http.client.HTTPConnection", Connection)
    out = UrlClient("http://fixture.example").request("POST", "/upload", ImageUpload(UPLOAD_PNG), 1)
    assert b'name="image"; filename="smoke.png"' in sent[0][1]["body"]
    assert UPLOAD_PNG in sent[0][1]["body"]
    assert sent[0][1]["headers"]["Content-Type"].startswith("multipart/form-data;")
    assert out.raw_body == UPLOAD_PNG


rig = service_support.rig


@pytest.mark.anyio
async def test_storage_intent_survives_settings_snapshot_and_approval(rig):
    service, source, calls = rig
    service.save_project_settings(
        "demo", {"default_targets": "both"}, updated_by="operator", expected_version=0
    )
    plan = service_support.plan("storage-approval")
    meta = {
        "intent": "create",
        "evidence": [{"file": "flaskr/uploads.py", "line": 3, "kind": "hardcoded_dir"}],
        "bucket": BUCKET,
    }
    ctx = RunContext(plan.run_id, project=plan.project, project_settings={"_infra_storage": meta})
    service.prepare(plan, ctx, source)
    assert service.approval_view(plan.run_id)["project_settings"]["_infra_storage"] == meta
    service.approve(plan.run_id, approver="operator")
    service.start(plan.run_id)
    result = await service.wait(plan.run_id)
    assert result.status is service_support.RunStatus.SUCCEEDED
    assert all(ctx.project_settings["_infra_storage"] == meta for _, ctx in calls.contexts)
