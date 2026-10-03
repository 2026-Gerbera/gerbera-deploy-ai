"""source=fixture: 순번은 삭제·오류 이후에도 증가하고 승인 입력에 고정된다."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ddak.cloud.infra.storage_allocation import prepare_storage_context
from ddak.core.config import AdapterMode
from ddak.core.contracts.errors import DdakToolError
from ddak.core.storage import bucket_name, valid_bucket
from ddak.core.storage_sequence import MAX_BUCKET_ATTEMPTS, reserve_bucket
from tests.unit.cloud.infra.test_storage_runtime import context


def test_create_remove_create_keeps_monotonic_names_and_run_reservation(tmp_path):
    probe = Mock(return_value=404)
    names = []
    for n in range(1, 4):
        ctx = replace(context(), run_id=f"run-{n}")
        prepared = prepare_storage_context(ctx, tmp_path, probe=probe)
        bucket = prepared.project_settings["_infra_storage"]["bucket"]
        names.append(bucket)
        assert prepare_storage_context(prepared, tmp_path, probe=probe) == prepared
        assert probe.call_count == n
        removed = replace(
            prepared,
            platform={"cloud": {"upload_bucket": bucket}},
            project_settings={"_infra_storage": {"intent": "remove", "evidence": []}},
        )
        assert prepare_storage_context(removed, tmp_path).project_settings["_infra_storage"] == {
            "intent": "remove",
            "bucket": bucket,
            "evidence": [],
        }
    assert names == [bucket_name("flaskr", n) for n in (1, 2, 3)]


def test_head_occupied_names_are_burned_and_retry_is_bounded(tmp_path):
    probe = Mock(side_effect=[403, 200, 404])
    assert reserve_bucket(tmp_path, "flaskr", "flaskr", "run-1", probe) == bucket_name("flaskr", 3)
    assert [call.args[0] for call in probe.call_args_list] == [
        bucket_name("flaskr", n) for n in (1, 2, 3)
    ]
    occupied = Mock(return_value=403)
    with pytest.raises(DdakToolError, match="사용 가능한"):
        reserve_bucket(tmp_path, "flaskr", "flaskr", "run-2", occupied)
    assert occupied.call_count == MAX_BUCKET_ATTEMPTS
    assert reserve_bucket(tmp_path, "flaskr", "flaskr", "run-3", lambda _: 404) == bucket_name(
        "flaskr", MAX_BUCKET_ATTEMPTS + 4
    )


def test_unknown_head_failure_does_not_reserve_or_reuse_number(tmp_path):
    with pytest.raises(DdakToolError, match="조회 실패"):
        reserve_bucket(tmp_path, "flaskr", "flaskr", "run-1", lambda _: 500)
    assert reserve_bucket(tmp_path, "flaskr", "flaskr", "run-1", lambda _: 404) == bucket_name(
        "flaskr", 2
    )


def test_remove_observes_output_and_does_not_reuse_number_on_new_controller(tmp_path):
    ctx = context("remove", bucket=bucket_name("flaskr", 17))
    prepare_storage_context(ctx, tmp_path)
    assert reserve_bucket(tmp_path, "flaskr", "flaskr", "run-2", lambda _: 404) == bucket_name(
        "flaskr", 18
    )
    forged = replace(ctx, platform={"cloud": {"upload_bucket": bucket_name("flaskr", 3)}})
    with pytest.raises(DdakToolError, match="현재 출력"):
        prepare_storage_context(forged, tmp_path)


def test_real_preparation_uses_read_session_and_head_only(tmp_path, monkeypatch):
    from ddak.cloud.infra import storage_allocation

    client = Mock()
    client.head_bucket.side_effect = [
        ClientError({"ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadBucket"),
        ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket"),
    ]
    session = Mock()
    session.client.return_value = client
    checked = Mock(return_value=session)
    monkeypatch.setattr(storage_allocation, "checked_session", checked)
    ctx = replace(context(), adapter_mode=AdapterMode.REAL)
    prepared = prepare_storage_context(ctx, tmp_path)
    assert prepared.project_settings["_infra_storage"]["bucket"] == bucket_name("flaskr", 2)
    checked.assert_called_once()
    assert session.client.call_args.args == ("s3",)
    assert [call[0] for call in client.mock_calls] == ["head_bucket", "head_bucket"]


@pytest.mark.parametrize("n", [0, -1, 1000000, True, "1"])
def test_bucket_sequence_format(n):
    with pytest.raises(ValueError):
        bucket_name("flaskr", n)


def test_bucket_is_exact_platform_and_bounded():
    assert valid_bucket("flaskr", bucket_name("flaskr", 999999))
    assert len(bucket_name("a" * 31, 999999)) <= 63
    assert not valid_bucket("flaskr", "gerbera-flaskr-images-1-images-2")
    assert not valid_bucket("flaskr", bucket_name("other", 1))
    assert not valid_bucket("flaskr", "gerbera-flaskr-images-01")
