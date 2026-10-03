"""compare_env_results 비교 로직(순수 함수). 담당 장민영(O3).

두 환경의 smoke_test 결과와 이미지 관측을 비교해 "같아야 하는데 다른 것"만 실패로 센다.
- 판정: match / mismatch / expected_diff / skipped
- 예상된 차이(환경이 달라서 정상적으로 다른 값)는 코드의 고정 목록이다. AI가 정하지 않는다.
- tool.py가 이번 run의 smoke 결과(verify/smoke 보관소)와 RunContext.release_artifacts를 넘긴다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from ddak.core.contracts.enums import Target
from ddak.core.contracts.release import ReleaseArtifacts
from ddak.core.contracts.tools.smoke_test import Observed, SmokeTestOutput

Verdict = Literal["match", "mismatch", "expected_diff", "skipped"]

# 환경이 달라서 다른 것이 정상인 관찰값(O3 문서 5-5 표 왼쪽, 골든패스 8-3). 키 → 이유.
# 키는 smoke 결과의 normalized 키(시나리오 이름을 뺀 것)다. 여기 없는 키는 다르면 모두 실패다
# (release_id, schema, db.dialect, 화면 지문 page.*, 상태 코드 등. 표 오른쪽).
# 주소·응답 시간·헤더·쿠키 값·스모크 글 개수는 smoke가 처음부터 싣지 않는다.
EXPECTED_DIFFS: Mapping[str, str] = {
    "app_env": "환경 이름(onprem/cloud)",
    "base_url": "환경별 공개 주소",
    "db.host": "환경별 DB 주소",
    "db.tls": "DB TLS(온프레미스는 평문 또는 검증 없는 TLS, 클라우드는 CA 검증 TLS)",
    "db.tls_verified": "DB 인증서 검증 여부(클라우드만 필수)",
    "secure": "쿠키 Secure 속성(공개 주소 스킴을 따름)",
}
EXPECTED_DIFF_KEYS = frozenset(EXPECTED_DIFFS)


@dataclass(frozen=True)
class Check:
    id: str
    verdict: Verdict
    local: Observed = None
    cloud: Observed = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"id": self.id, "verdict": self.verdict}
        if (
            self.verdict != "expected_diff"
        ):  # 예상된 차이는 값 대신 판정만 보인다(주소 등 노출 방지)
            out["local"] = self.local
            out["cloud"] = self.cloud
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Comparison:
    checks: list[Check] = field(default_factory=list)

    @property
    def unexpected_diffs(self) -> int:
        return sum(c.verdict == "mismatch" for c in self.checks)

    @property
    def passed(self) -> bool:
        return self.unexpected_diffs == 0 and any(c.verdict == "match" for c in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "unexpected_diffs": self.unexpected_diffs,
            "checks": [c.to_dict() for c in self.checks],
        }


def _norm(value: Observed) -> Observed:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _value_check(cid: str, key: str, local: Observed, cloud: Observed) -> Check:
    a, b = _norm(local), _norm(cloud)
    if key in EXPECTED_DIFFS:
        if a == b:
            return Check(cid, "match", a, b)
        return Check(cid, "expected_diff", a, b, f"예상된 차이: {EXPECTED_DIFFS[key]}")
    if a == b:
        return Check(cid, "match", a, b)
    return Check(cid, "mismatch", a, b, "두 환경 값이 다르다")


def compare_smoke(local: SmokeTestOutput, cloud: SmokeTestOutput) -> list[Check]:
    """시나리오별로 ok·status·normalized 값을 비교한다."""
    if local.target is not Target.LOCAL or cloud.target is not Target.CLOUD:
        raise ValueError("local·cloud 결과 순서가 맞지 않는다")
    if local.run_id != cloud.run_id:
        return [Check("run_id", "mismatch", local.run_id, cloud.run_id, "다른 run의 결과다")]
    checks: list[Check] = []
    by_id_local = {s.id: s for s in local.scenarios}
    by_id_cloud = {s.id: s for s in cloud.scenarios}
    for sid in list(dict.fromkeys([*by_id_local, *by_id_cloud])):
        a, b = by_id_local.get(sid), by_id_cloud.get(sid)
        if a is None or b is None:
            side = "local" if a is None else "cloud"
            checks.append(Check(sid, "mismatch", reason=f"{side}에 시나리오가 없다"))
            continue
        checks.append(_value_check(f"{sid}.ok", "ok", a.ok, b.ok))
        checks.append(_value_check(f"{sid}.status", "status", a.status, b.status))
        for key in dict.fromkeys([*a.normalized, *b.normalized]):
            checks.append(
                _value_check(f"{sid}.{key}", key, a.normalized.get(key), b.normalized.get(key))
            )
    return checks


def compare_images(artifacts: ReleaseArtifacts | None) -> list[Check]:
    """같은 이미지인지: 두 환경의 실제 digest가 같은 index의 플랫폼 항목에 속해야 한다.

    플랫폼 digest 자체는 amd64·arm64라 다를 수 있다(예상된 차이).
    """
    if artifacts is None:
        return [Check("image", "skipped", reason="빌드 산출물 기록이 없다")]
    checks: list[Check] = []
    for tier, image in sorted(artifacts.images.items()):
        members = set(image.platform_digests.values())
        seen: dict[str, Observed] = {}
        for target in (Target.LOCAL, Target.CLOUD):
            obs = artifacts.observations.get(target.value, {}).get(tier)
            seen[target.value] = obs.platform_digest if obs else None
        local, cloud = seen["local"], seen["cloud"]
        if local is None or cloud is None:
            checks.append(
                Check(f"image.{tier}.index", "mismatch", local, cloud, "실행 이미지 관측이 없다")
            )
            continue
        if local in members and cloud in members:
            checks.append(
                Check(f"image.{tier}.index", "match", image.index_digest, image.index_digest)
            )
        else:
            checks.append(
                Check(f"image.{tier}.index", "mismatch", local, cloud, "index에 없는 이미지다")
            )
        checks.append(
            Check(f"image.{tier}.platform", "match" if local == cloud else "expected_diff")
        )
    return checks


def compare_schema(signatures: Mapping[str, str | None] | None) -> list[Check]:
    """마이그레이션 러너 verify의 스키마 서명(S10). 이번 run에 DB step이 없으면 skipped."""
    if not signatures:
        return [Check("schema.signature", "skipped", reason="이번 run에 DB 확인 결과가 없다")]
    local, cloud = signatures.get("local"), signatures.get("cloud")
    if local is None and cloud is None:
        return [Check("schema.signature", "skipped", reason="이번 run에 DB 확인 결과가 없다")]
    return [_value_check("schema.signature", "signature", local, cloud)]


def _major_minor(version: str) -> str:
    return ".".join(version.split("-")[0].split(".")[:2])


def compare_db_fingerprint(fingerprints: Mapping[str, Mapping[str, str]] | None) -> list[Check]:
    """DB 지문(S12). 마이그레이션 러너 verify의 fingerprint(version·sql_mode·collation·
    time_zone·ssl_version)를 비교한다. 이번 run에 DB 확인 결과가 없으면 skipped.

    - sql_mode(순서 무관)·collation: 다르면 실패. 비엄격 sql_mode는 긴 값을 잘라 저장한다(R6).
    - version: major.minor가 다르면 실패, 패치 버전만 다르면 예상된 차이(RDS 자동 패치).
    - time_zone: 예상된 차이. 앱이 연결마다 time_zone='+00:00'을 건다(flaskr/db.py).
    - ssl_version: 클라우드는 비어 있으면 실패(RDS TLS 필수), 값 자체는 예상된 차이.
    """
    local = (fingerprints or {}).get("local")
    cloud = (fingerprints or {}).get("cloud")
    if not local or not cloud:
        return [Check("db.fingerprint", "skipped", reason="이번 run에 두 환경 DB 지문이 없다")]
    checks: list[Check] = []
    modes = [
        ",".join(sorted(filter(None, fp.get("sql_mode", "").split(",")))) for fp in (local, cloud)
    ]
    checks.append(_value_check("db.sql_mode", "sql_mode", *modes))
    checks.append(
        _value_check("db.collation", "collation", local.get("collation"), cloud.get("collation"))
    )
    lv, cv = str(local.get("version", "")), str(cloud.get("version", ""))
    if _major_minor(lv) != _major_minor(cv):
        checks.append(Check("db.version", "mismatch", lv, cv, "DB major.minor 버전이 다르다"))
    elif lv == cv:
        checks.append(Check("db.version", "match", lv, cv))
    else:
        checks.append(Check("db.version", "expected_diff", reason="예상된 차이: DB 패치 버전"))
    tz = local.get("time_zone"), cloud.get("time_zone")
    checks.append(
        Check("db.time_zone", "match", *tz)
        if tz[0] == tz[1]
        else Check(
            "db.time_zone", "expected_diff", reason="예상된 차이: 앱이 연결마다 UTC로 맞춘다"
        )
    )
    if not cloud.get("ssl_version"):
        checks.append(Check("db.ssl", "mismatch", reason="클라우드 DB 연결이 TLS가 아니다"))
    else:
        checks.append(Check("db.ssl", "expected_diff", reason="예상된 차이: DB TLS 버전"))
    return checks


def compare_env(
    local: SmokeTestOutput,
    cloud: SmokeTestOutput,
    *,
    artifacts: ReleaseArtifacts | None = None,
    schema_signatures: Mapping[str, str | None] | None = None,
    db_fingerprints: Mapping[str, Mapping[str, str]] | None = None,
) -> Comparison:
    """교차 비교: smoke 결과(정규화 화면 지문 포함) + 이미지 index + 스키마 서명 + DB 지문."""
    return Comparison(
        [
            *compare_smoke(local, cloud),
            *compare_images(artifacts),
            *compare_schema(schema_signatures),
            *compare_db_fingerprint(db_fingerprints),
        ]
    )
