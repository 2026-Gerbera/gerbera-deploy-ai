"""step 카탈로그: 가능한 step 전체(순수 데이터, AI·IO 없음).
근거는 fixtures/plans/golden_v2_update.json.

planner(생성)와 validate(검사)가 함께 읽는다. validate는 planner를 import할 수 없어 core에 둔다.
step id: build.<tier>, deploy.config|db|dbinit|storage.<env>, deploy.<tier>.<env>,
deploy.tls|infra|secrets.cloud, verify.health|smoke.<env>, verify.tls.cloud,
verify.compare|report|watch.cloud.

deploy.<tier>.<env> 순서는 deploy.yaml 순서가 아니라 고정 의존 순서(db → was → web)다. 업스트림을
먼저 올려 프런트 준비 확인이 새 업스트림을 보게 하고, 업스트림 실패 시 프런트는 건드리지 않는다.
모르는 tier는 deploy.yaml 순서대로 뒤에 둔다. 롤백은 실행기가 실제 호출 순서의 역순으로 한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field

from ddak.core.contracts.base import ContractModel, TierName
from ddak.core.contracts.enums import Effect, Layer, Target
from ddak.core.contracts.plan import SignalName

__all__ = ["FORBIDDEN_PARAM_KEYS", "StepDef", "catalog_steps"]

# plan.json params에 올 수 없는 키(V3). 값은 실행 컨텍스트에서 코드가 채운다.
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "domain", "hostname", "host", "url", "base_url", "endpoint", "zone",
        "image", "image_uri", "digest", "arn", "command", "cmd", "args", "shell",
    }
)  # fmt: skip


# 업스트림 먼저. 여기 없는 tier는 deploy.yaml 순서대로 뒤에 붙는다.
_DEPLOY_ORDER: tuple[str, ...] = ("db", "was", "web")


def _deploy_order(tiers: Sequence[TierName]) -> list[TierName]:
    return [t for t in _DEPLOY_ORDER if t in tiers] + [t for t in tiers if t not in _DEPLOY_ORDER]


class StepDef(ContractModel):
    id: str
    tool: str
    section: Literal["build", "deploy.local", "deploy.cloud", "verify"]
    target: Target | None
    tier: TierName | None
    layer: Layer
    effect: Effect
    allowed_params: tuple[str, ...] = ()
    default_params: dict[str, Any] = Field(default_factory=dict)
    wait_for: tuple[SignalName, ...] = ()
    signal: SignalName | None = None
    run: Literal["finally"] | None = None
    skip_rule: str | None = None  # 없으면 못 뺀다


def _env_steps(env: Literal["local", "cloud"], tiers: Sequence[TierName]) -> list[StepDef]:
    cloud = env == "cloud"
    tgt = Target.CLOUD if cloud else Target.LOCAL
    sec: Literal["deploy.local", "deploy.cloud"] = "deploy.cloud" if cloud else "deploy.local"
    image_ready: tuple[SignalName, ...] = ("images_ready",)

    def d(sid: str, tool: str, layer: Layer, effect: Effect, **kw: Any) -> StepDef:
        return StepDef(
            id=sid, tool=tool, section=sec, target=tgt, tier=kw.pop("tier", None),
            layer=layer, effect=effect, **kw,
        )  # fmt: skip

    C, M, OPT = Layer.CONDITIONAL, Layer.MANDATORY, Layer.OPTIONAL
    S, A, R = Effect.STATE_CHANGE, Effect.ADDITIVE_PREP, Effect.READ
    out: list[StepDef] = []
    if cloud:
        out += [
            d("deploy.tls.cloud", "ensure_tls", M, R, allowed_params=("mode",),
              default_params={"mode": "check"}),
            d("deploy.infra.cloud", "apply_infra", C, S,
              signal="infra_ready", skip_rule="no_infra_change"),
            d("deploy.secrets.cloud", "sync_env_to_cloud", C, A, allowed_params=("keys",),
              skip_rule="no_new_secret"),
        ]  # fmt: skip
    out.append(
        d(f"deploy.config.{env}", "inject_env_config", C, A if cloud else S,
          allowed_params=("keys",), wait_for=("images_ready",), skip_rule="no_new_keys")
    )  # fmt: skip
    out += [
        d(f"deploy.dbinit.{env}", "prepare_db", C, S, wait_for=image_ready,
          skip_rule="db_initialized"),
        d(f"deploy.migrate.{env}", "prepare_db", C, S, allowed_params=("migrations",),
          wait_for=image_ready, skip_rule="no_new_migrations"),
        d(f"deploy.storage.{env}", "prepare_storage", OPT, S, wait_for=image_ready,
          skip_rule="optional"),
    ]  # fmt: skip
    out += [
        d(f"deploy.{t}.{env}", "deploy_tier", C, S, tier=t, wait_for=image_ready,
          skip_rule="digest_deployed")
        for t in _deploy_order(tiers)
    ]  # fmt: skip
    out.append(d(f"verify.health.{env}", "health_check", M, R))
    if cloud:
        out.append(d("verify.tls.cloud", "verify_tls", M, R))
    out.append(
        d(f"verify.smoke.{env}", "smoke_test", M, R, allowed_params=("scenarios",),
          signal="cloud_verified" if cloud else "local_verified")
    )  # fmt: skip
    return out


def catalog_steps(
    tiers: Sequence[TierName], target: Literal["local", "cloud", "both"]
) -> list[StepDef]:
    """실행 순서대로 가능한 step 전체. 빼도 되는지는 layer·skip_rule이 정한다."""
    out = [
        StepDef(id=f"build.{t}", tool="build_image", section="build", target=None, tier=t,
                layer=Layer.CONDITIONAL, effect=Effect.ADDITIVE_PREP, skip_rule="tree_unchanged")
        for t in tiers
    ]  # fmt: skip
    envs: list[Literal["local", "cloud"]] = []
    if target in ("local", "both"):
        envs.append("local")
    if target in ("cloud", "both"):
        envs.append("cloud")
    for env in envs:
        out += _env_steps(env, tiers)
    waits: tuple[SignalName, ...] = tuple(
        "local_verified" if e == "local" else "cloud_verified" for e in envs
    )
    out.append(
        StepDef(id="verify.compare", tool="compare_env_results", section="verify", target=None,
                tier=None, layer=Layer.MANDATORY, effect=Effect.READ, wait_for=waits)
    )  # fmt: skip
    out.append(
        StepDef(id="verify.report", tool="post_report", section="verify", target=None, tier=None,
                layer=Layer.MANDATORY, effect=Effect.READ, run="finally")
    )  # fmt: skip
    if "cloud" in envs:
        out.append(
            StepDef(id="verify.watch.cloud", tool="watch_post_deploy", section="verify",
                    target=Target.CLOUD, tier=None, layer=Layer.OPTIONAL, effect=Effect.READ,
                    skip_rule="optional")
        )  # fmt: skip
    return out
