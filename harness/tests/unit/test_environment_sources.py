"""source=fixture: 승인 표의 출처와 실제 주입 키 목록을 비교한다. 외부 호출 없음."""

import ast
import inspect
import re

import pytest

from ddak.cloud.deploy import entry
from ddak.core.contracts.context import RunContext
from ddak.core.runtime_values import PIPELINE_KEYS
from ddak.executor.presentation import CLOUD_DEPLOY_ENV_KEYS, display_data
from ddak.onprem.deploy import config
from tests.unit.test_deployment_service import plan
from tests.unit.test_deployment_service import rig as rig
from tests.unit.test_ui_integration_fix10 import client_for

EXPECTED_CLOUD = {
    **dict.fromkeys(
        (
            "APP_BASE_URL",
            "APP_ENV",
            "MIGRATE_MODE",
            "PROXY_FIX_X_FOR",
            "PROXY_FIX_X_PROTO",
            "RELEASE_ID",
            "SOURCE_SHA",
            "SESSION_COOKIE_SECURE",
            "IMG_DIR",
        ),
        "배포 코드가 주입",
    ),
    "DATABASE_URL": "Secrets Manager(RDS 시크릿)",
    "SECRET_KEY": "배포 시 자동 생성·보관",
    "TF_ENV": "Terraform 출력",
    "TF_PLAIN": "Terraform 출력",
    "TF_SECRET": "Terraform 출력",
    "UNKNOWN_KEY": "출처 미확인",
}
HIDDEN = "fixture-hidden-value"


@pytest.fixture
def prepared(rig):
    service, source, _ = rig
    p = plan("env-sources")
    context = RunContext(
        p.run_id,
        project=p.project,
        targets="both",
        required_env_keys=tuple(EXPECTED_CLOUD),
        platform={
            "onprem": {
                "tiers": {
                    "was": {
                        "env_file": "/fixture/not-read",
                        "public_env": {"APP_ENV": HIDDEN, "RELEASE_ID": HIDDEN},
                    }
                }
            },
            "cloud": {
                "env": {"TF_ENV": HIDDEN, "APP_ENV": HIDDEN},
                "env_plain": {"TF_PLAIN": HIDDEN},
                "env_secret": {
                    "TF_SECRET": HIDDEN,
                    "DATABASE_URL": HIDDEN,
                    "SECRET_KEY": HIDDEN,
                },
            },
        },
    )
    rid = service.prepare(p, context, source)
    return service, rid


def test_cloud_sources_cover_runtime_secrets_terraform_and_unknown(prepared):
    service, rid = prepared
    rows = display_data(service.store, service.root, rid)["mappings"]
    assert {row["key"]: row["cloud"] for row in rows} == EXPECTED_CLOUD
    assert HIDDEN not in str(rows)


def test_onprem_runtime_keys_override_inventory_and_other_sources_stay(prepared):
    service, rid = prepared
    rows = {
        row["key"]: row["local"]
        for row in display_data(service.store, service.root, rid)["mappings"]
    }
    assert rows["RELEASE_ID"] == rows["SOURCE_SHA"] == "배포 코드가 주입"
    assert rows["APP_ENV"] == rows["IMG_DIR"] == "인벤토리"
    assert rows["SECRET_KEY"] == "배포 시 자동 생성·보관"
    assert (
        rows["DATABASE_URL"]
        == rows["UNKNOWN_KEY"]
        == "관리 페이지 · 저장 여부는 연결 검사에서 확인"
    )


def test_display_key_lists_match_deploy_entry_and_config():
    assert CLOUD_DEPLOY_ENV_KEYS == entry.RUNTIME_ENV_KEYS
    ctx = RunContext(
        "fixture",
        cloud_domain="app.example.test",
        platform={"cloud": {"upload_bucket": "gerbera-flaskr-images-3"}},
    )
    runtime = entry._runtime_environment(ctx, "fixture", "a" * 40)
    assert runtime["was"].keys() == CLOUD_DEPLOY_ENV_KEYS
    # 사용자 파일에서 제외하는 실제 키 집합과 비교한다. 파일은 열거나 실행하지 않는다.
    tree = ast.parse(inspect.getsource(config.inject_config))
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "host_keys" for target in node.targets
        )
    )
    excluded_keys = ast.literal_eval(assignment.value.generators[0].ifs[0].comparators[0])
    assert excluded_keys == PIPELINE_KEYS


@pytest.mark.parametrize("language", ("ko", "ja"))
def test_approval_source_labels_and_unknown_warning_render(prepared, language):
    service, rid = prepared
    with client_for(service) as client:
        response = client.get(f"/runs/{rid}/approval?lang={language}")
    assert response.status_code == 200
    table = response.text.split('id="environment-mapping"', 1)[1].split("</table>", 1)[0]
    rows = {
        key: body
        for key, body in re.findall(r"<tr><td><code>(.*?)</code></td>(.*?)</tr>", table, re.S)
    }
    injected = "배포 코드가 주입" if language == "ko" else "デプロイ処理が注入"
    database = (
        "Secrets Manager(RDS 시크릿)" if language == "ko" else "Secrets Manager(RDSシークレット)"
    )
    for key in CLOUD_DEPLOY_ENV_KEYS:
        assert injected in rows[key]
        assert 'class="state failure"' not in rows[key]
    assert database in rows["DATABASE_URL"]
    assert 'class="state failure"' in rows["UNKNOWN_KEY"]
    assert table.count('class="state failure"') == 1
    assert HIDDEN not in response.text
