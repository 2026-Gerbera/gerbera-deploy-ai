"""VM 준비 뒤 사용자가 명시적으로 실행하는 fixture 리허설. 기본 실행은 연결하지 않는다.

DDAK_TEST_VM=1 + INVENTORY/IMAGES/URL이 모두 있어야 한다. images JSON은
{v1: ImageArtifact, v2: ImageArtifact, broken: ImageArtifact} 형식이다.
실제 O3 앱·MySQL 검증이 아닌 원격 Docker 경로 검증이다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ddak.core.config import AdapterMode
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.release import ImageArtifact
from ddak.onprem.deploy import preflight_inventory
from tests.docker.service_rehearsal import service_rehearsals

_INPUTS = ("DDAK_TEST_VM_INVENTORY", "DDAK_TEST_VM_IMAGES", "DDAK_TEST_VM_URL")
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("DDAK_TEST_VM") != "1" or not all(os.environ.get(k) for k in _INPUTS),
        reason="VM·이미지 준비 알림 뒤 명시적 VM opt-in과 세 입력 필요",
    ),
]


def test_vm_fixture_update_failure_reset(tmp_path):
    inventory = json.loads(Path(os.environ["DDAK_TEST_VM_INVENTORY"]).read_text())
    assert inventory["mode"] == "vm"
    supplied = json.loads(Path(os.environ["DDAK_TEST_VM_IMAGES"]).read_text())
    artifacts = [ImageArtifact.model_validate(supplied[key]) for key in ("v1", "v2", "broken")]
    url = os.environ["DDAK_TEST_VM_URL"].rstrip("/") + "/version"
    assert url.startswith(("http://", "https://"))
    count = inventory["tiers"]["was"].get("replicas", 1)
    report = preflight_inventory(inventory)
    assert report["passed"], report

    def http(port, expected, *, secret_valid=True, release_id=None):
        del port
        end = time.monotonic() + 30
        consecutive = 0
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - 위에서 http(s) 제한
                    value = json.load(response)
                if (
                    value["version"] == expected
                    and value["secret_valid"] == secret_valid
                    and value["secret_present"] == secret_valid
                    and (release_id is None or value["release_id"] == release_id)
                ):
                    consecutive += 1
                    if consecutive >= count:
                        return value
                else:
                    consecutive = 0
            except (OSError, urllib.error.URLError, ValueError):
                consecutive = 0
            time.sleep(0.2)
        pytest.fail("VM 진입점의 연속 HTTP 버전 관측 실패")

    ctx = RunContext(
        "vm-rehearsal",
        project="flaskr",
        adapter_mode=AdapterMode.REAL,
        platform={"onprem": inventory},
    )
    reports = service_rehearsals(tmp_path / "vm", ctx, artifacts, 0, http)
    proof = Path(__file__).parents[2] / "var" / "validation" / "onprem-vm.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(
        json.dumps(
            {"source": "rehearsal", "preflight": report, "runs": reports},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
