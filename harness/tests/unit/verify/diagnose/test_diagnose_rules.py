"""diagnose 규칙 부분: 알려진 실패 문구 → 원인 범주. AI 없음, 원문은 결과에 싣지 않는다."""

from __future__ import annotations

import json
import time

import pytest

from ddak.core.contracts.enums import Target
from ddak.core.contracts.tools.compare_env_results import CompareCheck, CompareEnvResultsOutput
from ddak.core.contracts.tools.smoke_test import SmokeScenario, SmokeTestOutput
from ddak.verify.diagnose.rules import MAX_LOG_CHARS, diagnose_by_rules, rule_categories

RUN = "run-diag-1"
# 결과에 나오면 안 되는 값. 비밀값 스캐너에 걸리지 않게 이어 붙여 만든다(AGENTS 4절)
FAKE_SECRET = "not-a-real-" + "password-123"


@pytest.mark.parametrize(
    ("line", "rule", "category"),
    [
        (
            "flaskr.config.MissingEnvError: 필수 환경변수 SECRET_KEY가 없습니다",
            "missing_env",
            "secret_missing",
        ),
        ("KeyError: 'SECRET_KEY'", "missing_env", "secret_missing"),
        (
            "ResourceInitializationError: unable to pull secrets or registry auth",
            "ecs_resource_init",
            "secret_missing",
        ),
        (
            "CannotPullContainerError: pull image manifest has been retried",
            "image_pull",
            "image_pull",
        ),
        (
            "pymysql.err.OperationalError: (3159, 'Connections using insecure transport are "
            "prohibited while --require_secure_transport=ON.')",
            "db_insecure_transport",
            "db_tls",
        ),
        (
            "(2003, \"Can't connect to MySQL server on 'db' ([SSL: CERTIFICATE_VERIFY_FAILED] "
            'certificate verify failed: self-signed certificate in certificate chain)")',
            "db_cert_verify",
            "db_tls",
        ),
        (
            "(1045, \"Access denied for user 'flaskr_app'@'10.0.1.5' (using password: YES)\")",
            "db_access_denied",
            "db_conn",
        ),
        (
            "(2003, \"Can't connect to MySQL server on 'db' (timed out)\")",
            "db_unreachable",
            "db_conn",
        ),
        ("(1146, \"Table 'flaskr.post' doesn't exist\")", "db_missing_table", "db_schema"),
        (
            'MIGRATE_RESULT {"phase": "up", "ok": false, "current": null, "expected": "0002"}',
            "migrate_failed",
            "db_schema",
        ),
        (
            "ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] hostname mismatch",
            "tls_cert_verify",
            "tls",
        ),
        ("Target.ResponseCodeMismatch: Health checks failed", "lb_unhealthy", "health_timeout"),
        ("[ERROR] Worker failed to boot.", "worker_boot", "health_timeout"),
    ],
)
def test_log_signature_sets_category(line: str, rule: str, category: str) -> None:
    result = diagnose_by_rules(logs=[f"2026-10-03T00:00:00Z start\n{line}\n"])
    assert result.rule == rule and result.category == category
    assert result.is_hypothesis is False
    assert [e.to_dict() for e in result.evidence] == [{"source": "log", "ref": "log#2"}]
    assert rule_categories()[rule] == category


def test_db_cert_failure_beats_generic_connect_failure() -> None:
    # PyMySQL은 인증서 검증 실패도 "Can't connect to MySQL server"로 감싼다
    line = "(2003, \"Can't connect to MySQL server on 'db' ([SSL: CERTIFICATE_VERIFY_FAILED] x)\")"
    result = diagnose_by_rules(logs=[line])
    assert result.rule == "db_cert_verify"
    assert result.matched_rules == ["db_cert_verify"]  # 한 줄에는 가장 앞선 규칙 하나만


def test_matched_text_never_appears_in_result() -> None:
    line = f"(1045, \"Access denied for user 'app'@'host' (password={FAKE_SECRET})\")"
    result = diagnose_by_rules(logs=[line])
    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    assert result.category == "db_conn"
    assert FAKE_SECRET not in dumped and "host" not in dumped


@pytest.mark.parametrize(
    "line",
    ["listening on port 1045", "took 1146 ms", "request id 3159 done", "error count: 2003"],
)
def test_bare_error_numbers_do_not_match(line: str) -> None:
    assert diagnose_by_rules(logs=[line]).category == "unknown"


def _smoke(target: Target, scenarios: list[SmokeScenario], passed: bool = False) -> SmokeTestOutput:
    return SmokeTestOutput(
        run_id=RUN, target=target, passed=passed, elapsed_s=0.1, scenarios=scenarios
    )


def _ready(current: str | None, expected: str = "0002") -> SmokeScenario:
    normalized = {"status": "fail", "schema.current": current, "schema.expected": expected}
    return SmokeScenario(id="S0.ready", ok=False, status=503, normalized=normalized)


def test_ready_with_schema_behind_is_db_schema() -> None:
    result = diagnose_by_rules(smoke=[_smoke(Target.LOCAL, [_ready("0001")])])
    assert result.rule == "smoke_schema_behind" and result.category == "db_schema"
    assert result.evidence[0].to_dict() == {"source": "smoke", "ref": "local:S0.ready"}


def test_ready_without_schema_value_is_not_called_schema_problem() -> None:
    # DB 연결이 실패해도 current는 None이다. 스키마 문제로 단정하지 않는다
    result = diagnose_by_rules(smoke=[_smoke(Target.CLOUD, [_ready(None)])])
    assert result.rule == "smoke_not_ready" and result.category == "health_timeout"


def test_other_release_is_config_mismatch() -> None:
    version = SmokeScenario(id="S0.version", ok=False, status=200, detail="release_id가 다름")
    result = diagnose_by_rules(smoke=[_smoke(Target.CLOUD, [version])])
    assert result.rule == "smoke_other_release" and result.category == "config_mismatch"


def test_all_connection_failures_is_unreachable() -> None:
    down = [
        SmokeScenario(id=sid, ok=False, detail="연결 실패: ConnectionRefusedError")
        for sid in ("S0.version", "B1")
    ]
    result = diagnose_by_rules(smoke=[_smoke(Target.LOCAL, down)])
    assert "smoke_unreachable" in result.matched_rules
    assert result.category == "health_timeout"


def test_parity_mismatch_points_to_check_ids() -> None:
    compare = CompareEnvResultsOutput(
        run_id=RUN,
        passed=False,
        unexpected_diffs=1,
        elapsed_s=0.0,
        checks=[
            CompareCheck(id="S0.version.schema_expected", verdict="mismatch", local="1", cloud="2"),
            CompareCheck(id="S0.version.app_env", verdict="expected_diff"),
        ],
    )
    result = diagnose_by_rules(compare=compare)
    assert result.category == "parity_diff"
    assert [e.ref for e in result.evidence] == ["S0.version.schema_expected"]


def test_log_cause_comes_before_smoke_symptom() -> None:
    result = diagnose_by_rules(
        logs=["flaskr.config.MissingEnvError: 필수 환경변수 SECRET_KEY가 없습니다"],
        smoke=[_smoke(Target.LOCAL, [_ready(None)])],
    )
    assert result.rule == "missing_env"
    assert result.matched_rules == ["missing_env", "smoke_not_ready"]


def test_nothing_known_is_unknown_hypothesis() -> None:
    result = diagnose_by_rules(logs=["everything looks fine"])
    data = result.to_dict()
    assert data["category"] == "unknown" and data["is_hypothesis"] is True
    assert data["rule"] is None and data["evidence"] == []


def test_huge_log_is_bounded_and_only_tail_is_read() -> None:
    head = "(1146, \"Table 'flaskr.post' doesn't exist\")\n"  # 끝부분 밖이라 보지 않는다
    log = head + ("x" * 79 + "\n") * (MAX_LOG_CHARS // 80 * 4)
    started = time.monotonic()
    result = diagnose_by_rules(logs=[log])
    assert time.monotonic() - started < 2.0
    assert result.category == "unknown"
