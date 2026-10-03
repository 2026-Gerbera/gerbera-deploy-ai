"""대회 rolling 허용과 공통 속도·배포 소유권 제한을 검증한다."""

import pytest

from ddak.cloud.infra.policy import PolicyViolation, static_gate
from tests.unit.cloud.infra import test_bluegreen_policy as b
from tests.unit.cloud.infra.test_runtime import resource


def rolling_body(configuration=None):
    body = b.service_body()
    body.pop("deployment_configuration")
    body["load_balancer"][0].pop("advanced_configuration")
    if configuration is not None:
        body["deployment_configuration"] = configuration
    return body


def rolling_rule():
    return {"action": [{"type": "forward", "target_group_arn": b.TG_BLUE}]}


def bundle(body, target=None, rule=None):
    resources = [
        ("aws_ecs_service", "app", body),
        ("aws_lb_target_group", "app", target if target is not None else b.target_body()),
    ]
    if rule is not None:
        resources.append(("aws_lb_listener_rule", "app", rule))
    # 리소스마다 다른 파일을 사용해 입력 순서·파일 분리에도 전략을 정확히 판정한다.
    return {
        f"part{i}.tf": b.source(kind, values) for i, (kind, _name, values) in enumerate(resources)
    }, resources


def check_both(body, *, target=None, rule=None, action=None, error=None):
    files, items = bundle(body, target, rule)
    result = static_gate(files, layer="platform")
    plan = [resource(kind, name, action or ["create"], values) for kind, name, values in items]
    if error:
        assert not result.passed and error in result.detail
        with pytest.raises(PolicyViolation, match=error):
            b.summarize(plan)
    else:
        assert result.passed, result.detail
        assert b.summarize(plan)


@pytest.mark.parametrize("configuration", [None, [], [{}], [{"strategy": "ROLLING"}]])
@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
def test_rolling_without_advanced_or_second_target_passes(configuration, action):
    check_both(rolling_body(configuration), rule=rolling_rule(), action=action)


@pytest.mark.parametrize(
    "field,value,rule",
    [
        ("deregistration_delay", 300, "TARGET_GROUP_DEREGISTRATION"),
        ("interval", 30, "TARGET_GROUP_INTERVAL"),
        ("healthy_threshold", 5, "TARGET_GROUP_HEALTHY_THRESHOLD"),
    ],
)
def test_speed_limits_apply_to_rolling(field, value, rule):
    body = rolling_body()
    target = b.target_body()
    (target if field == "deregistration_delay" else target["health_check"][0])[field] = value
    check_both(body, target=target, action=["no-op"], error=rule)


@pytest.mark.parametrize("controller", ["CODE_DEPLOY", "EXTERNAL"])
def test_non_ecs_controller_is_rejected_for_rolling(controller):
    body = rolling_body()
    body["deployment_controller"] = [{"type": controller}]
    check_both(body, error="ECS_CONTROLLER")


@pytest.mark.parametrize("field", ["enable", "rollback"])
def test_rolling_circuit_breaker_is_still_required(field):
    body = rolling_body()
    body["deployment_circuit_breaker"][0][field] = False
    check_both(body, error="ECS_CIRCUIT_BREAKER")


@pytest.mark.parametrize(
    "field,expected",
    [
        ("deployment_minimum_healthy_percent", 100),
        ("deployment_maximum_percent", 200),
    ],
)
def test_optional_rolling_percentages_accept_exact_policy(field, expected):
    body = rolling_body()
    body[field] = expected
    check_both(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_minimum_healthy_percent", 50),
        ("deployment_maximum_percent", 100),
        ("deployment_minimum_healthy_percent", "100"),
        ("deployment_maximum_percent", True),
        ("deployment_maximum_percent", None),
    ],
)
def test_explicit_rolling_percentages_cannot_weaken_policy(field, value):
    body = rolling_body()
    body[field] = value
    check_both(body, error="ECS_ROLLING_PERCENTAGES")


@pytest.mark.parametrize(
    "field,rule",
    [
        ("deployment_minimum_healthy_percent", "ECS_ROLLING_PERCENTAGES"),
        ("deployment_maximum_percent", "ECS_ROLLING_PERCENTAGES"),
        ("deployment_configuration", "ECS_DEPLOYMENT_STRATEGY"),
        ("deployment_controller", "ECS_CONTROLLER"),
        ("deployment_circuit_breaker", "ECS_CIRCUIT_BREAKER"),
    ],
)
def test_unknown_rolling_fields_are_not_omission(field, rule):
    item = resource("aws_ecs_service", "app", ["no-op"], rolling_body())
    item["change"]["after_unknown"] = {field: True}
    with pytest.raises(PolicyViolation, match=rule):
        b.summarize([item])


def test_unknown_strategy_cannot_fall_back_to_rolling():
    item = resource("aws_ecs_service", "app", ["no-op"], rolling_body([{}]))
    item["change"]["after_unknown"] = {"deployment_configuration": [{"strategy": True}]}
    with pytest.raises(PolicyViolation, match="ECS_DEPLOYMENT_STRATEGY"):
        b.summarize([item])


def test_rolling_service_still_preserves_deployment_ownership():
    body = rolling_body()
    body.pop("lifecycle")
    result = static_gate(bundle(body)[0], layer="platform")
    assert not result.passed and result.detail == "ECS_DEPLOYMENT_OWNED_BY_C2"
