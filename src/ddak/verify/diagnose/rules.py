"""diagnose_parity_gap의 규칙 부분(AI 없음). 담당 장민영(O3).

알려진 실패 문구(규칙 시그니처)로 원인 범주를 먼저 정한다. 규칙이 못 잡은 경우에만
나중에 AI(call_ai) 설명을 붙인다(O3 문서 6-3, 10/2 변경사항 "나중": 규칙 부분부터).

- 입력(로그·smoke 결과·비교 결과)은 신뢰하지 않는 데이터다. 정규식으로 찾기만 하고
  지시로 읽지 않는다.
- 근거(evidence)에는 위치만 남긴다(log#줄 번호, smoke:시나리오 id, diff:검사 id).
  일치한 원문은 싣지 않는다(비밀값·접속 문자열이 섞일 수 있다).
- 판정 순서: 로그 시그니처(구체적인 원인) → smoke 신호 → 비교 불일치 → unknown(가설).
- 툴 진입점은 advisory.py(diagnose_parity_gap), 등록은 tool.py다.
  범주 목록은 계약 모델에서 가져온다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ddak.core.contracts.tools.compare_env_results import CompareEnvResultsOutput
from ddak.core.contracts.tools.diagnose_parity_gap import Category
from ddak.core.contracts.tools.smoke_test import SmokeTestOutput

EvidenceSource = Literal["log", "smoke", "diff"]

MAX_LOG_CHARS = 64 * 1024  # 로그 하나에서 보는 양(끝부분). 앞부분은 버린다
MAX_LOGS = 20
MAX_EVIDENCE = 5


@dataclass(frozen=True)
class Rule:
    id: str
    category: Category
    pattern: re.Pattern[str]
    summary: str
    suggested_next: str


def _rule(rid: str, category: Category, pattern: str, summary: str, next_step: str) -> Rule:
    return Rule(rid, category, re.compile(pattern, re.IGNORECASE), summary, next_step)


# 순서가 우선순위다. 같은 줄에 여러 규칙이 맞을 수 있으므로 구체적인 것을 먼저 둔다
# (예: DB 인증서 검증 실패는 "Can't connect to MySQL server" 문구를 함께 담는다).
LOG_RULES: tuple[Rule, ...] = (
    _rule(
        "missing_env",
        "secret_missing",
        r"MissingEnvError|필수 환경변수 [A-Za-z_][A-Za-z0-9_]{0,63}\s*가 없습니다"
        r"|KeyError: '[A-Z][A-Z0-9_]{1,63}'",
        "필수 환경변수(비밀값 포함)가 주입되지 않아 앱이 기동하지 못했다",
        "이 환경의 설정 주입(inject_env_config)과 시크릿 값이 있는지 확인한다",
    ),
    _rule(
        "ecs_resource_init",
        "secret_missing",
        r"ResourceInitializationError",
        "ECS 태스크가 시크릿 또는 레지스트리 인증 정보를 읽지 못했다(IAM 전파 전일 수 있다)",
        "실행 역할의 시크릿 읽기 권한과 시크릿 ARN을 확인하고, 전파를 기다린 뒤 다시 배포한다",
    ),
    _rule(
        "image_pull",
        "image_pull",
        r"CannotPullContainerError|pull access denied|manifest unknown|ErrImagePull",
        "실행 환경이 이미지를 받지 못했다",
        "이미지 저장소 이름·digest와 레지스트리 인증(Docker Hub 토큰)을 확인한다",
    ),
    _rule(
        "db_insecure_transport",
        "db_tls",
        r"\(3159,|ERROR 3159|insecure transport are prohibited",
        "DB 서버가 TLS를 요구하는데 TLS 없이 연결했다",
        "DATABASE_URL의 ssl_ca 지정과 이미지 안 CA 번들(certs/global-bundle.pem)을 확인한다",
    ),
    _rule(
        "db_cert_verify",
        "db_tls",
        r"Can't connect to MySQL server.{0,300}CERTIFICATE_VERIFY_FAILED",
        "DB 서버 인증서를 CA 번들로 검증하지 못했다",
        "CA 번들이 RDS 번들인지, 접속 호스트가 RDS 엔드포인트 이름인지(IP·별칭 금지) 확인한다",
    ),
    _rule(
        "db_access_denied",
        "db_conn",
        r"\(1045,|ERROR 1045|Access denied for user",
        "DB 계정 인증에 실패했다",
        "이 환경의 DB 계정·비밀번호 주입과 계정 권한을 확인한다",
    ),
    _rule(
        "db_unreachable",
        "db_conn",
        r"Can't connect to MySQL server|Unknown MySQL server host|Lost connection to MySQL"
        r"|\(200[35],",
        "앱이 DB 서버에 연결하지 못했다",
        "DB 주소·포트, 보안 그룹·네트워크, DB 기동 상태를 확인한다",
    ),
    _rule(
        "db_missing_table",
        "db_schema",
        r"\((?:1146|1054),|ERROR (?:1146|1054)|Table '[^'\n]{1,128}' doesn't exist"
        r"|Unknown column",
        "DB 스키마가 앱이 기대하는 버전보다 뒤처졌다(마이그레이션 누락 가능)",
        "마이그레이션 precheck·verify의 current·expected와 prepare_db 실행 여부를 확인한다",
    ),
    _rule(
        "migrate_failed",
        "db_schema",
        r"MIGRATE_RESULT \{[^\n]{0,4000}\"ok\": ?false",
        "마이그레이션 러너가 실패를 보고했다",
        "MIGRATE_RESULT의 phase와 러너 진단 문구(stderr)를 확인한다",
    ),
    _rule(
        "tls_cert_verify",
        "tls",
        r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLCertVerificationError",
        "HTTPS 인증서를 검증하지 못했다",
        "도메인 인증서(ACM) 발급·DNS 검증 상태와 접속 도메인을 확인한다",
    ),
    _rule(
        "lb_unhealthy",
        "health_timeout",
        r"Target\.(?:ResponseCodeMismatch|Timeout|FailedHealthChecks)|unhealthy targets?",
        "로드밸런서 헬스 체크를 통과하지 못했다",
        "헬스 경로(/health/ready) 응답과 컨테이너 기동 로그를 확인한다",
    ),
    _rule(
        "worker_boot",
        "health_timeout",
        r"Worker failed to boot|Application object must be callable",
        "앱 프로세스(gunicorn)가 기동하지 못했다",
        "was 컨테이너 기동 로그의 첫 예외를 확인한다",
    ),
)


@dataclass(frozen=True)
class Evidence:
    source: EvidenceSource
    ref: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "ref": self.ref}


@dataclass(frozen=True)
class RuleDiagnosis:
    """O3 문서 6-3 원인 설명 모양 + 어느 규칙이 정했는지."""

    category: Category
    summary: str
    suggested_next: str
    rule: str | None  # 정한 규칙 id. None이면 규칙이 못 잡았다(unknown)
    evidence: list[Evidence] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)  # 맞은 규칙 전부(우선순위 순)

    @property
    def is_hypothesis(self) -> bool:
        # 규칙 시그니처가 맞은 경우는 근거가 있다. 못 잡은 경우만 가설이다
        return self.rule is None

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "summary": self.summary[:400],
            "evidence": [e.to_dict() for e in self.evidence[:MAX_EVIDENCE]],
            "is_hypothesis": self.is_hypothesis,
            "suggested_next": self.suggested_next[:200],
            "rule": self.rule,
            "matched_rules": self.matched_rules,
        }


@dataclass(frozen=True)
class _Hit:
    rule: Rule
    evidence: Evidence


def _log_hits(logs: Sequence[str]) -> list[_Hit]:
    hits: list[_Hit] = []
    for index, text in enumerate(logs[:MAX_LOGS]):
        tail = text[-MAX_LOG_CHARS:] if isinstance(text, str) else ""
        for number, line in enumerate(tail.splitlines(), start=1):
            for rule in LOG_RULES:
                if rule.pattern.search(line):
                    ref = f"log{index}#{number}" if len(logs) > 1 else f"log#{number}"
                    hits.append(_Hit(rule, Evidence("log", ref)))
                    break  # 한 줄에는 가장 앞선 규칙 하나만
    return hits


def _smoke_hits(results: Iterable[SmokeTestOutput]) -> list[_Hit]:
    hits: list[_Hit] = []
    for out in results:
        where = out.target.value
        by_id = {s.id: s for s in out.scenarios}
        ready = by_id.get("S0.ready")
        version = by_id.get("S0.version")
        if ready is not None and not ready.ok:
            current = ready.normalized.get("schema.current")
            expected = ready.normalized.get("schema.expected")
            # current가 None이면 DB 연결 실패일 수도 있어 스키마 뒤처짐으로 단정하지 않는다
            behind = isinstance(expected, str) and isinstance(current, str) and current < expected
            rule = _SMOKE_SCHEMA_BEHIND if behind else _SMOKE_NOT_READY
            hits.append(_Hit(rule, Evidence("smoke", f"{where}:S0.ready")))
        if version is not None and not version.ok and version.status == 200:
            hits.append(_Hit(_SMOKE_OTHER_RELEASE, Evidence("smoke", f"{where}:S0.version")))
        if out.scenarios and all(s.detail.startswith("연결 실패") for s in out.scenarios):
            hits.append(_Hit(_SMOKE_UNREACHABLE, Evidence("smoke", f"{where}:*")))
    return hits


_SMOKE_SCHEMA_BEHIND = _rule(
    "smoke_schema_behind",
    "db_schema",
    r"(?!)",
    "준비 확인(/health/ready)이 스키마가 기대 버전보다 뒤처졌다고 보고했다",
    "이 환경의 마이그레이션(prepare_db)이 실행·통과했는지 확인한다",
)
_SMOKE_NOT_READY = _rule(
    "smoke_not_ready",
    "health_timeout",
    r"(?!)",
    "앱이 준비 상태(/health/ready)가 아니다",
    "/health/ready 응답의 db·tls 항목과 was 기동 로그를 확인한다",
)
_SMOKE_OTHER_RELEASE = _rule(
    "smoke_other_release",
    "config_mismatch",
    r"(?!)",
    "응답한 앱의 릴리스·DB 종류가 이번 배포와 다르다(이전 버전이 떠 있을 수 있다)",
    "이 환경에 실제로 떠 있는 이미지 digest와 RELEASE_ID 주입을 확인한다",
)
_SMOKE_UNREACHABLE = _rule(
    "smoke_unreachable",
    "health_timeout",
    r"(?!)",
    "smoke가 앱에 한 번도 연결하지 못했다",
    "공개 주소(public_url·도메인)와 web 컨테이너·터널·로드밸런서 상태를 확인한다",
)
_PARITY = _rule(
    "parity_mismatch",
    "parity_diff",
    r"(?!)",
    "두 환경이 같아야 하는 값에서 다르다",
    "불일치 검사 항목의 두 환경 값을 비교하고, 이미지 digest·스키마 서명부터 확인한다",
)


def _compare_hits(compare: CompareEnvResultsOutput | None) -> list[_Hit]:
    if compare is None or compare.passed:
        return []
    return [
        _Hit(_PARITY, Evidence("diff", check.id))
        for check in compare.checks
        if check.verdict == "mismatch"
    ]


def diagnose_by_rules(
    logs: Sequence[str] = (),
    smoke: Iterable[SmokeTestOutput] = (),
    compare: CompareEnvResultsOutput | None = None,
) -> RuleDiagnosis:
    """규칙으로 원인 범주를 정한다. 못 잡으면 category=unknown(가설)."""
    hits = [*_log_hits(logs), *_smoke_hits(smoke), *_compare_hits(compare)]
    if not hits:
        return RuleDiagnosis(
            category="unknown",
            summary="알려진 실패 문구를 찾지 못했다",
            suggested_next="로그 발췌와 실패 단계를 확인한다(AI 설명 대상)",
            rule=None,
        )
    first = hits[0].rule
    same = [h.evidence for h in hits if h.rule.id == first.id]
    return RuleDiagnosis(
        category=first.category,
        summary=first.summary,
        suggested_next=first.suggested_next,
        rule=first.id,
        evidence=same[:MAX_EVIDENCE],
        matched_rules=list(dict.fromkeys(h.rule.id for h in hits)),
    )


def rule_categories() -> Mapping[str, Category]:
    """규칙 id → 범주(문서·테스트용)."""
    rules = (*LOG_RULES, _SMOKE_SCHEMA_BEHIND, _SMOKE_NOT_READY, _SMOKE_OTHER_RELEASE)
    return {r.id: r.category for r in (*rules, _SMOKE_UNREACHABLE, _PARITY)}
