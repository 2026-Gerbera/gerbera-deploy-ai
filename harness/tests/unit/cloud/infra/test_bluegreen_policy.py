"""BLUE_GREEN HCL/plan 검사. 실제 AWS나 Terraform 실행 없이 검증한다."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from ddak.cloud.infra.plan import summarize_plan
from ddak.cloud.infra.policy import PolicyViolation, static_gate
from tests.unit.cloud.infra.test_runtime import ACCOUNT, SETTINGS, resource

TG_BLUE = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:targetgroup/blue/1111"
TG_GREEN = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:targetgroup/green/2222"
RULE = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:listener-rule/app/demo/11/22/33"


def service_body():
    return {
        "deployment_circuit_breaker": [{"enable": True, "rollback": True}],
        "deployment_configuration": [{"strategy": "BLUE_GREEN", "bake_time_in_minutes": 1}],
        "deployment_controller": [{"type": "ECS"}],
        "load_balancer": [
            {
                "target_group_arn": TG_BLUE,
                "advanced_configuration": [
                    {
                        "alternate_target_group_arn": TG_GREEN,
                        "production_listener_rule": RULE,
                        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/ddak/infra/ddak-ecs-infra-elb",
                    }
                ],
            }
        ],
        "lifecycle": [{"ignore_changes": ["task_definition", "desired_count"]}],
    }


def listener_body():
    return {
        "action": [
            {
                "type": "forward",
                "forward": [
                    {
                        "target_group": [
                            {"arn": TG_BLUE, "weight": 100},
                            {"arn": TG_GREEN, "weight": 0},
                        ]
                    }
                ],
            }
        ],
        "lifecycle": [{"ignore_changes": ["action"]}],
    }


def target_body():
    return {"deregistration_delay": 30, "health_check": [{"interval": 10, "healthy_threshold": 3}]}


def hcl_body(body):
    lines = []
    for key, value in body.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            lines.extend(key + " {\n" + hcl_body(v) + "\n}" for v in value)
        else:
            lines.append(key + " = " + json.dumps(value))
    return "\n".join(lines)


def source(kind, body):
    text = hcl_body(body).replace(f"arn:aws:iam::{ACCOUNT}:", "arn:aws:iam::${var.account_id}:")
    return f'resource "{kind}" "app" {{\n{text}\n}}'


def summarize(items, configs=None, **kwargs):
    return summarize_plan(
        {
            "format_version": "1.2",
            "resource_changes": items,
            "configuration": {"root_module": {"resources": configs or []}},
        },
        layer="platform",
        plan_sha256="sha256:" + "a" * 64,
        exit_code=2,
        account_id=ACCOUNT,
        boundary_arn=SETTINGS.boundary_arn,
        update=False,
        analyzer=Mock(validate_policy=Mock(return_value={"findings": []})),
        checkov={"passed": True},
        **kwargs,
    )


def set_path(body, path, value):
    for key in path[:-1]:
        body = body[key]
    if value is REMOVE:
        body.pop(path[-1])
    else:
        body[path[-1]] = value


REMOVE = object()
DEPLOY = ("deployment_configuration", 0)
ADVANCED = ("load_balancer", 0, "advanced_configuration", 0)
FORWARD = ("action", 0, "forward", 0, "target_group")


@pytest.mark.parametrize(
    "kind,factory",
    [
        ("aws_ecs_service", service_body),
        ("aws_lb_listener_rule", listener_body),
        ("aws_lb_target_group", target_body),
    ],
)
@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
def test_bluegreen_valid_hcl_and_plan(kind, factory, action):
    body = factory()
    assert static_gate({"main.tf": source(kind, body)}, layer="platform").passed
    body.pop("lifecycle", None)  # plan after에는 lifecycle이 없다.
    assert summarize([resource(kind, "app", action, body)])


@pytest.mark.parametrize(
    "path,value,rule",
    [
        (("deployment_configuration",), {}, "ECS_DEPLOYMENT_STRATEGY"),
        (
            (
                *DEPLOY,
                "strategy",
            ),
            "CANARY",
            "ECS_DEPLOYMENT_STRATEGY",
        ),
        (
            (
                *DEPLOY,
                "strategy",
            ),
            None,
            "ECS_DEPLOYMENT_STRATEGY",
        ),
        (
            (
                *DEPLOY,
                "bake_time_in_minutes",
            ),
            REMOVE,
            "ECS_BAKE_TIME",
        ),
        (
            (
                *DEPLOY,
                "bake_time_in_minutes",
            ),
            -1,
            "ECS_BAKE_TIME",
        ),
        (
            (
                *DEPLOY,
                "bake_time_in_minutes",
            ),
            3,
            "ECS_BAKE_TIME",
        ),
        (
            (
                *DEPLOY,
                "bake_time_in_minutes",
            ),
            True,
            "ECS_BAKE_TIME",
        ),
        (
            (
                *DEPLOY,
                "bake_time_in_minutes",
            ),
            "1",
            "ECS_BAKE_TIME",
        ),
        (("deployment_controller", 0, "type"), "CODE_DEPLOY", "ECS_CONTROLLER"),
        (("deployment_controller", 0, "type"), "EXTERNAL", "ECS_CONTROLLER"),
        (("deployment_controller", 0, "type"), REMOVE, "ECS_CONTROLLER"),
        (("load_balancer",), REMOVE, "ECS_ADVANCED_CONFIGURATION"),
        (("load_balancer", 0, "advanced_configuration"), REMOVE, "ECS_ADVANCED_CONFIGURATION"),
        (
            (
                *ADVANCED,
                "alternate_target_group_arn",
            ),
            REMOVE,
            "ECS_ADVANCED_CONFIGURATION",
        ),
        (
            (
                *ADVANCED,
                "alternate_target_group_arn",
            ),
            "",
            "ECS_ADVANCED_CONFIGURATION",
        ),
        (
            (
                *ADVANCED,
                "production_listener_rule",
            ),
            REMOVE,
            "ECS_ADVANCED_CONFIGURATION",
        ),
        (
            (
                *ADVANCED,
                "production_listener_rule",
            ),
            "not-an-arn",
            "ECS_ADVANCED_CONFIGURATION",
        ),
        (
            (
                *ADVANCED,
                "role_arn",
            ),
            REMOVE,
            "ECS_INFRA_ROLE",
        ),
        (
            (
                *ADVANCED,
                "role_arn",
            ),
            f"arn:aws:iam::{ACCOUNT}:role/ddak/infra/other",
            "ECS_INFRA_ROLE",
        ),
        (
            (
                *ADVANCED,
                "role_arn",
            ),
            "arn:aws:iam::999999999999:role/ddak/infra/ddak-ecs-infra-elb",
            "ECS_INFRA_ROLE",
        ),
        (
            (
                *ADVANCED,
                "role_arn",
            ),
            "${aws_iam_role.infra.arn}",
            "ECS_INFRA_ROLE",
        ),
        (
            (
                *DEPLOY,
                "lifecycle_hook",
            ),
            [{"hook_target_arn": "fixture"}],
            "LIFECYCLE_HOOK",
        ),
        (("lifecycle_hook",), [{"hook_target_arn": "fixture"}], "LIFECYCLE_HOOK"),
    ],
)
@pytest.mark.parametrize("action", [["create"], ["no-op"]])
def test_service_rejects_invalid_hcl_and_plan(path, value, rule, action):
    body = service_body()
    set_path(body, path, value)
    result = static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform")
    assert not result.passed and rule in result.detail
    with pytest.raises(PolicyViolation, match=rule):
        summarize([resource("aws_ecs_service", "app", action, body)])


@pytest.mark.parametrize("bake", [0, 1, 2])
@pytest.mark.parametrize("controller", [None, [], [{"type": "ECS"}]])
def test_bake_boundaries_and_optional_controller(bake, controller):
    body = service_body()
    body["deployment_configuration"][0]["bake_time_in_minutes"] = bake
    if controller is None:
        body.pop("deployment_controller")
    else:
        body["deployment_controller"] = controller
    assert static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform").passed
    assert summarize([resource("aws_ecs_service", "app", ["create"], body)])


@pytest.mark.parametrize(
    "path,value,rule",
    [
        (("action",), REMOVE, "LISTENER_FORWARD_TARGETS"),
        (("action", 0, "type"), "redirect", "LISTENER_FORWARD_TARGETS"),
        (("action", 0, "forward"), REMOVE, "LISTENER_FORWARD_TARGETS"),
        (FORWARD, [{"arn": TG_BLUE}], "LISTENER_FORWARD_TARGETS"),
        (FORWARD, [{"arn": TG_BLUE}] * 2, "LISTENER_FORWARD_TARGETS"),
        (
            FORWARD,
            [{"arn": TG_BLUE}, {"arn": TG_GREEN}, {"arn": TG_BLUE}],
            "LISTENER_FORWARD_TARGETS",
        ),
        ((*FORWARD, 1, "arn"), None, "LISTENER_FORWARD_TARGETS"),
        ((*FORWARD, 1, "arn"), "fixture", "LISTENER_FORWARD_TARGETS"),
    ],
)
@pytest.mark.parametrize("action", [["create"], ["no-op"]])
def test_listener_requires_two_distinct_forward_targets(path, value, rule, action):
    body = listener_body()
    set_path(body, path, value)
    result = static_gate({"main.tf": source("aws_lb_listener_rule", body)}, layer="platform")
    assert not result.passed and result.detail == rule
    with pytest.raises(PolicyViolation, match=rule):
        summarize([resource("aws_lb_listener_rule", "app", action, body)])


@pytest.mark.parametrize(
    "ignored,valid",
    [
        (["action"], True),
        (["action", "tags"], True),
        ([], False),
        (["tags"], False),
        ("all", False),
    ],
)
def test_listener_ignore_changes_hcl_only(ignored, valid):
    body = listener_body()
    body["lifecycle"][0]["ignore_changes"] = ignored
    result = static_gate({"main.tf": source("aws_lb_listener_rule", body)}, layer="platform")
    assert result.passed is valid
    if not valid:
        assert result.detail == "LISTENER_ACTION_OWNED_BY_C2"
    body.pop("lifecycle")
    assert summarize([resource("aws_lb_listener_rule", "app", ["no-op"], body)])


@pytest.mark.parametrize(
    "path,value,rule",
    [
        (("deregistration_delay",), 31, "TARGET_GROUP_DEREGISTRATION"),
        (("deregistration_delay",), REMOVE, "TARGET_GROUP_DEREGISTRATION"),
        (("deregistration_delay",), True, "TARGET_GROUP_DEREGISTRATION"),
        (("health_check",), REMOVE, "TARGET_GROUP_HEALTH_CHECK"),
        (("health_check", 0, "interval"), 11, "TARGET_GROUP_INTERVAL"),
        (("health_check", 0, "interval"), REMOVE, "TARGET_GROUP_INTERVAL"),
        (("health_check", 0, "interval"), True, "TARGET_GROUP_INTERVAL"),
        (("health_check", 0, "healthy_threshold"), 4, "TARGET_GROUP_HEALTHY_THRESHOLD"),
        (("health_check", 0, "healthy_threshold"), REMOVE, "TARGET_GROUP_HEALTHY_THRESHOLD"),
        (("health_check", 0, "healthy_threshold"), True, "TARGET_GROUP_HEALTHY_THRESHOLD"),
    ],
)
@pytest.mark.parametrize("action", [["create"], ["no-op"]])
def test_target_group_limits_hcl_and_plan(path, value, rule, action):
    body = target_body()
    set_path(body, path, value)
    result = static_gate({"main.tf": source("aws_lb_target_group", body)}, layer="platform")
    assert not result.passed and result.detail == rule
    with pytest.raises(PolicyViolation, match=rule):
        summarize([resource("aws_lb_target_group", "app", action, body)])


def first_create():
    blue = resource("aws_lb_target_group", "blue", ["create"], target_body())
    green = resource("aws_lb_target_group", "green", ["create"], target_body())
    for item in (blue, green):
        item["change"]["after_unknown"] = {"arn": True}
    listener = resource("aws_lb_listener_rule", "main", ["create"], listener_body())
    listener["change"]["after_unknown"] = {
        "arn": True,
        "action": [{"forward": [{"target_group": [{"arn": True}, {"arn": True}]}]}],
    }
    for group in listener["change"]["after"]["action"][0]["forward"][0]["target_group"]:
        group["arn"] = None
    service = resource("aws_ecs_service", "app", ["create"], service_body())
    advanced = service["change"]["after"]["load_balancer"][0]["advanced_configuration"][0]
    advanced["alternate_target_group_arn"] = advanced["production_listener_rule"] = None
    service["change"]["after_unknown"] = {
        "load_balancer": [
            {
                "advanced_configuration": [
                    {
                        "alternate_target_group_arn": True,
                        "production_listener_rule": True,
                    }
                ]
            }
        ]
    }
    configs = [
        {
            "address": listener["address"],
            "type": listener["type"],
            "mode": "managed",
            "expressions": {
                "action": [
                    {
                        "forward": [
                            {
                                "target_group": [
                                    {
                                        "arn": {
                                            "references": [
                                                "aws_lb_target_group.blue.arn",
                                                "aws_lb_target_group.blue",
                                            ]
                                        }
                                    },
                                    {"arn": {"references": ["aws_lb_target_group.green.arn"]}},
                                ]
                            }
                        ]
                    }
                ]
            },
        },
        {
            "address": service["address"],
            "type": service["type"],
            "mode": "managed",
            "expressions": {
                "load_balancer": [
                    {
                        "advanced_configuration": [
                            {
                                "alternate_target_group_arn": {
                                    "references": ["aws_lb_target_group.green.arn"]
                                },
                                "production_listener_rule": {
                                    "references": ["aws_lb_listener_rule.main.arn"]
                                },
                            }
                        ]
                    }
                ]
            },
        },
    ]
    return [blue, green, listener, service], configs


def test_first_create_resolves_unknown_arns_without_mutating_plan():
    items, configs = first_create()
    original = deepcopy(items)
    assert summarize(items, configs)["counts"]["create"] == 4
    assert items == original


@pytest.mark.parametrize(
    "references",
    [
        [],
        ["aws_lb_target_group.missing.arn"],
        ["aws_lb_target_group.green.id"],
        ["data.aws_lb_target_group.green.arn"],
        ["aws_lb_target_group.blue.arn", "aws_lb_target_group.green.arn"],
        ["aws_lb_target_group.green.arn", "var.override"],
        ["aws_lb_target_group.green.arn", "local.suffix"],
        ["aws_lb_target_group.green.arn", "aws_lb_target_group.green.name"],
    ],
)
def test_unknown_arn_needs_single_verified_reference(references):
    items, configs = first_create()
    configs[1]["expressions"]["load_balancer"][0]["advanced_configuration"][0][
        "alternate_target_group_arn"
    ] = {"references": references}
    with pytest.raises(PolicyViolation, match="BLUE_GREEN_REFERENCE_UNRESOLVED"):
        summarize(items, configs)


def test_unknown_without_configuration_fails():
    items, _ = first_create()
    with pytest.raises(PolicyViolation, match="BLUE_GREEN_REFERENCE_UNRESOLVED"):
        summarize(items)


def test_unknown_forward_targets_must_be_distinct():
    items, configs = first_create()
    groups = configs[0]["expressions"]["action"][0]["forward"][0]["target_group"]
    groups[1] = deepcopy(groups[0])
    with pytest.raises(PolicyViolation, match="LISTENER_FORWARD_TARGETS"):
        summarize(items, configs)


@pytest.mark.parametrize(
    "field,rule",
    [
        ("strategy", "ECS_DEPLOYMENT_STRATEGY"),
        ("bake_time_in_minutes", "ECS_BAKE_TIME"),
        ("lifecycle_hook", "LIFECYCLE_HOOK"),
    ],
)
def test_unknown_deployment_fields_do_not_use_known_looking_after(field, rule):
    item = resource("aws_ecs_service", "app", ["no-op"], service_body())
    item["change"]["after_unknown"] = {"deployment_configuration": [{field: True}]}
    with pytest.raises(PolicyViolation, match=rule):
        summarize([item])


@pytest.mark.parametrize(
    "field,rule",
    [
        ("deployment_controller", "ECS_CONTROLLER"),
        ("deployment_circuit_breaker", "ECS_CIRCUIT_BREAKER"),
        ("load_balancer", "ECS_ADVANCED"),
    ],
)
def test_unknown_service_blocks_fail(field, rule):
    item = resource("aws_ecs_service", "app", ["no-op"], service_body())
    item["change"]["after_unknown"] = {field: True}
    with pytest.raises(PolicyViolation, match=rule):
        summarize([item])


def test_unknown_role_arn_is_never_inferred():
    items, configs = first_create()
    items[-1]["change"]["after_unknown"]["load_balancer"][0]["advanced_configuration"][0][
        "role_arn"
    ] = True
    with pytest.raises(PolicyViolation, match="ECS_INFRA_ROLE"):
        summarize(items, configs)


def test_unknown_reference_target_is_itself_validated_on_noop():
    items, configs = first_create()
    items[1]["change"]["actions"] = ["no-op"]
    items[1]["change"]["after"]["deregistration_delay"] = 300
    with pytest.raises(PolicyViolation, match="TARGET_GROUP_DEREGISTRATION"):
        summarize(items, configs)


def test_hcl_uses_variable_account_but_plan_requires_resolved_account():
    body = service_body()
    literal_source = f'resource "aws_ecs_service" "app" {{\n{hcl_body(body)}\n}}'
    assert static_gate({"main.tf": literal_source}, layer="platform").detail == "ECS_INFRA_ROLE"
    body["load_balancer"][0]["advanced_configuration"][0]["role_arn"] = (
        "arn:aws:iam::${var.account_id}:role/ddak/infra/ddak-ecs-infra-elb"
    )
    with pytest.raises(PolicyViolation, match="ECS_INFRA_ROLE"):
        summarize([resource("aws_ecs_service", "app", ["create"], body)])


@pytest.mark.parametrize("delay", [0, 30, "30"])
def test_target_group_deregistration_valid_numeric_encodings(delay):
    body = target_body()
    body["deregistration_delay"] = delay
    assert static_gate({"main.tf": source("aws_lb_target_group", body)}, layer="platform").passed
    assert summarize([resource("aws_lb_target_group", "app", ["create"], body)])


def test_hcl_direct_arn_references_are_allowed():
    body = service_body()
    advanced = body["load_balancer"][0]["advanced_configuration"][0]
    advanced["alternate_target_group_arn"] = "${aws_lb_target_group.green.arn}"
    advanced["production_listener_rule"] = "${aws_lb_listener_rule.main.arn}"
    assert static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform").passed
    body = listener_body()
    groups = body["action"][0]["forward"][0]["target_group"]
    groups[0]["arn"] = "${aws_lb_target_group.blue.arn}"
    groups[1]["arn"] = "${aws_lb_target_group.green.arn}"
    assert static_gate({"main.tf": source("aws_lb_listener_rule", body)}, layer="platform").passed


@pytest.mark.parametrize(
    "reference",
    [
        "${aws_lb_target_group.green.arn}-suffix",
        "prefix-${aws_lb_target_group.green.arn}",
        "${aws_lb_target_group.green.arn}${aws_lb_target_group.green.arn}",
        "${aws_lb_target_group.green.name}",
    ],
)
def test_static_gate_rejects_arn_expressions_that_reference_lists_cannot_disambiguate(reference):
    body = service_body()
    body["load_balancer"][0]["advanced_configuration"][0]["alternate_target_group_arn"] = reference
    result = static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform")
    assert result.detail == "ECS_ADVANCED_CONFIGURATION"


@pytest.mark.parametrize("target_state", ["deleted", "wrong-mode", "missing-arn"])
def test_unknown_reference_requires_existing_managed_target_with_arn(target_state):
    items, configs = first_create()
    if target_state == "deleted":
        items[1]["change"].update(actions=["delete"], after=None)
    elif target_state == "wrong-mode":
        items[1]["mode"] = "data"
    else:
        items[1]["change"]["after_unknown"].pop("arn")
    with pytest.raises(
        PolicyViolation, match=r"BLUE_GREEN_REFERENCE_UNRESOLVED|PLAN_RESOURCE_MODE"
    ):
        summarize(items, configs)


def test_unknown_ref_can_resolve_to_an_already_known_target_arn():
    items, configs = first_create()
    for item, arn in zip(items[:2], (TG_BLUE, TG_GREEN), strict=True):
        item["change"]["after_unknown"].pop("arn")
        item["change"]["after"]["arn"] = arn
    assert summarize(items, configs)["counts"]["create"] == 4


def test_different_addresses_with_same_resolved_arn_are_not_distinct_targets():
    items, configs = first_create()
    for item in items[:2]:
        item["change"]["after_unknown"].pop("arn")
        item["change"]["after"]["arn"] = TG_BLUE
    with pytest.raises(PolicyViolation, match="LISTENER_FORWARD_TARGETS"):
        summarize(items, configs)


@pytest.mark.parametrize("field", ["environment", "environmentFiles", "secrets"])
@pytest.mark.parametrize("action", [["create"], ["no-op"]])
def test_container_inputs_remain_owned_by_c2(field, action):
    body = {
        "container_definitions": json.dumps(
            [
                {
                    "name": "web",
                    "image": "fixture",
                    field: [{"name": "FIXTURE", "value": "fixture"}],
                }
            ]
        )
    }
    result = static_gate({"main.tf": source("aws_ecs_task_definition", body)}, layer="platform")
    assert result.detail == "CONTAINER_ENV_MANAGED_BY_C2"
    with pytest.raises(PolicyViolation, match="CONTAINER_ENV_MANAGED_BY_C2"):
        summarize([resource("aws_ecs_task_definition", "app", action, body)])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize("test_rule", [REMOVE, None, ""])
def test_service_allows_only_absent_test_listener_in_hcl_and_plan(action, test_rule):
    body = service_body()
    advanced = body["load_balancer"][0]["advanced_configuration"][0]
    if test_rule is not REMOVE:
        advanced["test_listener_rule"] = test_rule
    assert static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform").passed
    assert summarize([resource("aws_ecs_service", "app", action, body)])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize(
    "test_rule",
    [
        RULE,
        "${aws_lb_listener_rule.test.arn}",
        " ",
        "invalid",
        False,
        True,
        0,
        1,
        [],
        {},
        [{"arn": RULE}],
    ],
)
def test_service_rejects_test_listener_and_malformed_values_in_hcl_and_plan(action, test_rule):
    body = service_body()
    body["load_balancer"][0]["advanced_configuration"][0]["test_listener_rule"] = test_rule
    result = static_gate({"main.tf": source("aws_ecs_service", body)}, layer="platform")
    assert result.detail == "ECS_TEST_LISTENER_FORBIDDEN"
    with pytest.raises(PolicyViolation, match="ECS_TEST_LISTENER_FORBIDDEN"):
        summarize([resource("aws_ecs_service", "app", action, body)])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize("after_value", [REMOVE, None, ""])
def test_service_rejects_unknown_test_listener_even_when_after_looks_absent(action, after_value):
    body = service_body()
    if after_value is not REMOVE:
        body["load_balancer"][0]["advanced_configuration"][0]["test_listener_rule"] = after_value
    item = resource("aws_ecs_service", "app", action, body)
    item["change"]["after_unknown"] = {
        "load_balancer": [{"advanced_configuration": [{"test_listener_rule": True}]}],
    }
    with pytest.raises(PolicyViolation, match="ECS_TEST_LISTENER_FORBIDDEN"):
        summarize([item])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize("weights", [(1, 0), (100, 0), (999, 0), (0, 1), (0, 100), (0, 999)])
def test_listener_allows_all_traffic_on_either_target_in_hcl_and_plan(action, weights):
    body = listener_body()
    groups = body["action"][0]["forward"][0]["target_group"]
    for group, weight in zip(groups, weights, strict=True):
        group["weight"] = weight
    assert static_gate({"main.tf": source("aws_lb_listener_rule", body)}, layer="platform").passed
    assert summarize([resource("aws_lb_listener_rule", "app", action, body)])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize(
    "weights",
    [
        (REMOVE, REMOVE),
        (REMOVE, 0),
        (1, REMOVE),
        (1, 1),
        (50, 50),
        (0, 0),
        (-1, 0),
        (0, -1),
        (1000, 0),
        (0, 1000),
        (True, 0),
        (1, False),
        (False, 1),
        (0, True),
        (None, 0),
        (1, None),
        ("100", 0),
        (1.5, 0),
    ],
)
def test_listener_rejects_missing_split_empty_and_malformed_weights_in_hcl_and_plan(
    action, weights
):
    body = listener_body()
    groups = body["action"][0]["forward"][0]["target_group"]
    for group, weight in zip(groups, weights, strict=True):
        if weight is REMOVE:
            group.pop("weight")
        else:
            group["weight"] = weight
    result = static_gate({"main.tf": source("aws_lb_listener_rule", body)}, layer="platform")
    assert result.detail == "LISTENER_ALL_AT_ONCE_WEIGHTS"
    with pytest.raises(PolicyViolation, match="LISTENER_ALL_AT_ONCE_WEIGHTS"):
        summarize([resource("aws_lb_listener_rule", "app", action, body)])


@pytest.mark.parametrize("action", [["create"], ["update"], ["no-op"]])
@pytest.mark.parametrize("unknown_index", [0, 1])
@pytest.mark.parametrize("keep_after_value", [False, True])
def test_listener_rejects_unknown_weight_even_with_valid_looking_after(
    action, unknown_index, keep_after_value
):
    body = listener_body()
    groups = body["action"][0]["forward"][0]["target_group"]
    if not keep_after_value:
        groups[unknown_index].pop("weight")
    item = resource("aws_lb_listener_rule", "app", action, body)
    unknown_groups = [{}, {}]
    unknown_groups[unknown_index]["weight"] = True
    item["change"]["after_unknown"] = {
        "action": [{"forward": [{"target_group": unknown_groups}]}],
    }
    with pytest.raises(PolicyViolation, match="LISTENER_ALL_AT_ONCE_WEIGHTS"):
        summarize([item])
