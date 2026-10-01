"""compare_env_results 비교 로직. 결과 모델을 직접 만들어 smoke 모듈과 독립적으로 본다."""

from __future__ import annotations

from typing import Any

import pytest

from ddak.core.contracts.release import ReleaseArtifacts
from ddak.core.contracts.tools.smoke_test import SmokeScenario, SmokeTestOutput
from ddak.verify.compare.logic import compare_env, compare_images, compare_smoke

RUN = "run-cmp-1"
AMD, ARM, INDEX, SRC = ("sha256:" + c * 64 for c in "abcd")


def smoke(target: str, **overrides: Any) -> SmokeTestOutput:
    """두 환경에서 정상적으로 나올 v1 결과. overrides로 시나리오별 normalized 값을 바꾼다."""
    env = {"local": "onprem", "cloud": "cloud"}[target]
    tls_verified = target == "cloud"
    scenarios = {
        "S0.version": (
            200,
            {
                "release_id": RUN,
                "schema_expected": "0001",
                "app_env": env,
                "db.dialect": "mysql",
                "db.tls": True,
                "db.tls_verified": tls_verified,
            },
        ),
        "S0.ready": (200, {"status": "ok", "schema.current": "0001", "schema.expected": "0001"}),
        "B1": (200, {"status": 200, "cookie": None}),
        "B2.create": (302, {"status": 302, "location": "/", "listed": True, "cookie": None}),
    }
    out = []
    for sid, (status, normalized) in scenarios.items():
        changed = {**normalized, **overrides.get(sid, {})}
        ok = overrides.get(f"{sid}.ok", True)
        out.append(SmokeScenario(id=sid, ok=ok, status=status, normalized=changed))
    return SmokeTestOutput(
        run_id=overrides.get("run_id", RUN),
        target=target,
        passed=True,
        elapsed_s=0.1,
        scenarios=out,
    )


def artifacts(*, cloud_observed: bool = True) -> ReleaseArtifacts:
    observations: dict[str, Any] = {
        "local": {"was": {"platform": "linux/arm64", "platform_digest": ARM}},
    }
    if cloud_observed:
        observations["cloud"] = {"was": {"platform": "linux/amd64", "platform_digest": AMD}}
    return ReleaseArtifacts.model_validate(
        {
            "snapshot": {"source_snapshot_hash": SRC, "build_snapshot_hash": SRC},
            "images": {
                "was": {
                    "ref": f"docker.io/gerbera/was@{INDEX}",
                    "index_digest": INDEX,
                    "platform_digests": {"linux/amd64": AMD, "linux/arm64": ARM},
                }
            },
            "observations": observations,
        }
    )


def verdicts(checks: list[Any]) -> dict[str, str]:
    return {c.id: c.verdict for c in checks}


def test_same_app_passes_with_expected_env_differences() -> None:
    result = compare_env(smoke("local"), smoke("cloud"), artifacts=artifacts())
    assert result.passed is True and result.unexpected_diffs == 0
    v = verdicts(result.checks)
    assert v["S0.version.release_id"] == "match"
    assert v["S0.version.app_env"] == "expected_diff"
    assert v["S0.version.db.tls_verified"] == "expected_diff"
    assert v["image.was.index"] == "match"
    assert v["image.was.platform"] == "expected_diff"  # arm64 ≠ amd64는 정상
    assert v["schema.signature"] == "skipped"


def test_expected_diff_hides_values_in_report() -> None:
    result = compare_env(smoke("local"), smoke("cloud"))
    row = next(c for c in result.to_dict()["checks"] if c["id"] == "S0.version.app_env")  # type: ignore[union-attr]
    assert row == {"id": "S0.version.app_env", "verdict": "expected_diff"}


@pytest.mark.parametrize(
    ("overrides", "check_id"),
    [
        ({"S0.version": {"release_id": "rel-old"}}, "S0.version.release_id"),
        ({"S0.ready": {"schema.current": "0000"}}, "S0.ready.schema.current"),
        ({"S0.version": {"db.dialect": "sqlite"}}, "S0.version.db.dialect"),
        ({"B2.create": {"location": "/auth/login"}}, "B2.create.location"),
        ({"B1": {"cookie": "session"}}, "B1.cookie"),
        ({"B1.ok": False}, "B1.ok"),
    ],
)
def test_unexpected_difference_fails(overrides: dict[str, Any], check_id: str) -> None:
    result = compare_env(smoke("local"), smoke("cloud", **overrides))
    assert result.passed is False
    assert verdicts(result.checks)[check_id] == "mismatch"


def test_whitespace_only_difference_is_match() -> None:
    result = compare_smoke(
        smoke("local", **{"S0.ready": {"status": "ok "}}), smoke("cloud", **{"S0.ready": {}})
    )
    assert verdicts(result)["S0.ready.status"] == "match"


def test_secure_cookie_attribute_is_expected_difference() -> None:
    local = smoke("local", **{"B1": {"cookie": "session", "secure": False}})
    cloud = smoke("cloud", **{"B1": {"cookie": "session", "secure": True}})
    assert verdicts(compare_smoke(local, cloud))["B1.secure"] == "expected_diff"


def test_missing_scenario_and_other_run_are_mismatch() -> None:
    cloud = smoke("cloud")
    fewer = cloud.model_copy(update={"scenarios": cloud.scenarios[:-1]})
    assert verdicts(compare_smoke(smoke("local"), fewer))["B2.create"] == "mismatch"
    other = compare_smoke(smoke("local"), smoke("cloud", run_id="run-other"))
    assert verdicts(other) == {"run_id": "mismatch"}


def test_arguments_must_be_local_then_cloud() -> None:
    with pytest.raises(ValueError):
        compare_smoke(smoke("cloud"), smoke("local"))


def test_missing_image_observation_is_mismatch() -> None:
    checks = compare_images(artifacts(cloud_observed=False))
    assert verdicts(checks)["image.was.index"] == "mismatch"


def test_schema_signature() -> None:
    same = compare_env(
        smoke("local"), smoke("cloud"), schema_signatures={"local": SRC, "cloud": SRC}
    )
    assert verdicts(same.checks)["schema.signature"] == "match"
    differ = compare_env(
        smoke("local"), smoke("cloud"), schema_signatures={"local": SRC, "cloud": AMD}
    )
    assert differ.passed is False


def test_empty_comparison_does_not_pass() -> None:
    empty_local = smoke("local").model_copy(update={"scenarios": []})
    empty_cloud = smoke("cloud").model_copy(update={"scenarios": []})
    assert compare_env(empty_local, empty_cloud).passed is False
