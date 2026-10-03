"""C1 코드 소유 AWS 규칙. 서울·허용 리소스·검사 목록·앱 권한 경계."""

from __future__ import annotations

NAME = "aws"
REGION = "ap-northeast-2"  # 서울만
STATE_LAYERS = ("platform", "app")  # 💭 플랫폼 층(VPC·ALB·ECS 클러스터·공유 RDS·CodeBuild) / 앱 층

# 생성기에도 제공하는 C1 내부 규칙. 공유 카탈로그/RunContext 계약은 바꾸지 않는다.
RESOURCE_TYPES = frozenset(
    [
        "aws_vpc",
        "aws_subnet",
        "aws_internet_gateway",
        "aws_route_table",
        "aws_route_table_association",
        "aws_route",
        "aws_security_group",
        "aws_vpc_security_group_ingress_rule",
        "aws_vpc_security_group_egress_rule",
        "aws_lb",
        "aws_lb_listener",
        "aws_lb_listener_rule",
        "aws_lb_target_group",
        "aws_ecs_cluster",
        "aws_ecs_service",
        "aws_ecs_task_definition",
        "aws_db_subnet_group",
        "aws_db_parameter_group",
        "aws_db_instance",
        "aws_cloudwatch_log_group",
        "aws_codebuild_project",
        "aws_s3_bucket",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_public_access_block",
        "aws_secretsmanager_secret",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_acm_certificate",
        "aws_acm_certificate_validation",
        "aws_route53_record",
    ]
)
APP_RESOURCE_TYPES = frozenset({"aws_secretsmanager_secret", "aws_iam_role", "aws_iam_role_policy"})
CHECKS = tuple(
    f"CKV_AWS_{n}" for n in (17, 24, 25, 260, 277, 382, 62, 1, 63, 355, 286, 289, 290, 61, 293)
)
BOUNDARY_PATH = "/ddak/boundary/"
BOUNDARY_NAME = "ddak-app-boundary"
ECS_INFRA_ROLE_NAME = "ddak-ecs-infra-elb"
ECS_INFRA_ROLE_PATH = "/ddak/infra/"
ECS_INFRA_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForLoadBalancers"


def ecs_infrastructure_role(account_id: str) -> dict:
    """SDK가 확보하는 고정 역할. 생성 HCL에는 이 ARN 참조만 허용한다."""
    return {
        "name": ECS_INFRA_ROLE_NAME,
        "path": ECS_INFRA_ROLE_PATH,
        "arn": f"arn:aws:iam::{account_id}:role{ECS_INFRA_ROLE_PATH}{ECS_INFRA_ROLE_NAME}",
        "trust_policy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ecs.amazonaws.com"},
                }
            ],
        },
        "managed_policy_arn": ECS_INFRA_POLICY_ARN,
    }


def build_boundary_document(account_id: str) -> dict:
    """앱 경계와 분리한 CodeBuild 상한. 소스 버킷 권한은 C2 계약 확정 후 추가한다."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": (
                    f"arn:aws:secretsmanager:{REGION}:{account_id}:secret:"
                    "ddak-platform/dockerhub-push-??????"
                ),
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{account_id}:log-group:/aws/codebuild/ddak-*:*",
            },
        ],
    }


def bootstrap_dbinit_exception(account_id: str) -> dict:
    """신규 RDS의 ARN 확정 전, 승인에 포함할 유일한 dbinit 예외."""
    return {
        "layer": "platform",
        "mode": "bootstrap",
        "role_address": "aws_iam_role.dbinit_execution",
        "actions": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
        "resource": f"arn:aws:secretsmanager:{REGION}:{account_id}:secret:rds!db-*",
    }


def boundary_document(account_id: str) -> dict:
    """research/IAM §13-3의 앱 경계. 권한 자체를 주는 정책은 아니다."""
    prefix = f"arn:aws:secretsmanager:{REGION}:{account_id}:secret:"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": prefix + "ddak-platform/dockerhub-pull-??????",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{account_id}:log-group:/aws/ecs/ddak-*:*",
            },
            {
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": [prefix + "ddak/*", prefix + "rds!*"],
            },
            {
                "Effect": "Allow",
                "Action": "secretsmanager:DescribeSecret",
                "Resource": bootstrap_dbinit_exception(account_id)["resource"],
                "Condition": {
                    "ArnLike": {
                        "aws:PrincipalArn": (
                            f"arn:aws:iam::{account_id}:role/ddak/app/ddak-*-dbinit-exec"
                        )
                    }
                },
            },
            {
                "Effect": "Deny",
                "Action": ["iam:*", "organizations:*", "account:*", "sts:AssumeRole"],
                "Resource": "*",
            },
        ],
    }


def start_build_policy(
    account_id: str, project_name: str, repo_url: str, *, source_location: str | None = None
) -> dict:
    """외부 deployer 역할용 코드 소유 정책 설계. AI 생성 번들·자동 역할 생성에 넣지 않는다."""
    import re
    from urllib.parse import urlsplit

    url = urlsplit(repo_url)
    if (
        not re.fullmatch(r"\d{12}", account_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,254}", project_name)
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or any(c.isspace() for c in repo_url)
    ):
        raise ValueError("CodeBuild 정책 대상 형식 오류")
    if source_location is not None and not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9_./-]+", source_location
    ):
        raise ValueError("승인 S3 소스 위치 형식 오류")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "codebuild:StartBuild",
                "Resource": f"arn:aws:codebuild:{REGION}:{account_id}:project/{project_name}",
                "Condition": {
                    "ForAllValues:StringEquals": {
                        "codebuild:environment.environmentVariables.name": [
                            "BUILD_TIERS",
                            "RELEASE_ID",
                            "SOURCE_REVISION",
                            "IMAGE_REPO",
                        ],
                    },
                    "StringEquals": {"codebuild:source.location": source_location or repo_url},
                    "Null": {
                        "codebuild:environment.environmentVariables.name": "false",
                        "codebuild:source.buildspec": "false",
                        "codebuild:serviceRole": "true",
                    },
                },
            }
        ],
    }
