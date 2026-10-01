"""compare_env_results 비교 로직(순수 함수). 담당 장민영(O3).

두 환경의 smoke_test 결과와 이미지 관측을 비교해 "같아야 하는데 다른 것"만 실패로 센다.
- 판정: match / mismatch / expected_diff / skipped
- 예상된 차이(환경이 달라서 정상적으로 다른 값)는 코드의 고정 목록이다. AI가 정하지 않는다.
- 아직 툴로 등록하지 않았다. 실행기가 두 환경 결과를 넘기는 방법이 정해지면
  tool.py에서 이 함수를 부른다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from ddak.core.contracts.enums import Target
from ddak.core.contracts.release import ReleaseArtifacts
from ddak.core.contracts.tools.smoke_test import Observed, SmokeTestOutput

Verdict = Literal["match", "mismatch", "expected_diff", "skipped"]

# 환경이 달라서 다른 것이 정상인 관찰값(O3 문서 5-5, 골든패스 8-3).
# 키는 smoke 결과의 normalized 키(시나리오 이름을 뺀 것)다.
EXPECTED_DIFF_KEYS = frozenset(
    {
        "app_env",  # onprem / cloud
        "db.tls",  # 온프렘은 검증 없는 TLS 또는 평문, 클라우드는 CA 검증 TLS
        "db.tls_verified",
        "secure",  # 쿠키 Secure: 클라우드(HTTPS)만 켬
    }
)


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
    if key in EXPECTED_DIFF_KEYS:
        return Check(cid, "match" if a == b else "expected_diff", a, b)
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


def compare_env(
    local: SmokeTestOutput,
    cloud: SmokeTestOutput,
    *,
    artifacts: ReleaseArtifacts | None = None,
    schema_signatures: Mapping[str, str | None] | None = None,
) -> Comparison:
    """P0 교차 비교: smoke 결과 + 이미지 index + 스키마 서명."""
    return Comparison(
        [
            *compare_smoke(local, cloud),
            *compare_images(artifacts),
            *compare_schema(schema_signatures),
        ]
    )
