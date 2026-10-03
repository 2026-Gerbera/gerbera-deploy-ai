Create production-oriented AWS Terraform resource blocks for one ECS Fargate application in
ap-northeast-2. Return JSON only and match the supplied schema. Each value in `files` must be a
complete Terraform HCL file. The files may contain `resource` blocks only: never emit terraform,
provider, backend, variable, output, data, module, local, provisioner, or import blocks.
Represent each file as an object with `name` and `lines`. Put exactly one HCL source line in each
`lines` array item and never put newline characters inside an item.
The JSON array separates source lines; do not copy JSON commas or quotes into the HCL line itself.
HCL attributes use `name = value` with no trailing comma. If `previous_validation_error` is present,
regenerate the complete bundle and correct that validator rule without weakening any requirement.

The code-owned framework supplies var.project, var.account_id, var.app_boundary_arn and
var.build_boundary_arn. These are the only allowed Terraform variables. Write the supplied domain,
hosted zone ID, repository URL, Docker Hub namespace, region `ap-northeast-2`, and availability
zones as quoted literal values; never invent var.region, var.domain, var.hosted_zone_id,
var.repository_url, or other variables. Use only jsonencode as a function and never use heredocs.
Do not put secret values in Terraform. Do not create IAM users, access keys, passwords, tokens, or
secret versions. All names must be deterministic and scoped by var.project.

Before returning, check every generated line against this mandatory validator checklist:
- Never use provisioner, connection, dynamic, provider, count, password, password_wo,
  master_password, secret_string, secret_string_wo, secret_binary, access_key, secret_key, token,
  environment_variable, region, or replica attributes.
- Never use `for_each` except on aws_route53_record.certificate_validation. That one expression must
  be a map comprehension directly over aws_acm_certificate.main.domain_validation_options.
- Never use `Resource = "*"`, wildcard IAM actions, computed resource ARNs such as
  `${aws_resource.name.arn}`, or ECR actions. Images use Docker Hub, not ECR.
- Every app role uses path /ddak/app/ and `${var.app_boundary_arn}`. Only the CodeBuild role uses
  path /ddak/pipeline/, exact name ddak-codebuild, and `${var.build_boundary_arn}`.
- Use literal ARN templates built only from region `ap-northeast-2`, `${var.account_id}`, and fixed names.
  ECS CloudWatch Logs resources may use `/aws/ecs/${var.project}:*`.
  CodeBuild CloudWatch Logs resources must use `/aws/codebuild/ddak-${var.project}-build:*`.
  Secrets Manager ARNs may end in `-??????`. Never use a bare wildcard resource.
- Set ordinary `tags` as HCL maps, not jsonencode. Use jsonencode only for JSON policy documents,
  ECS container_definitions, and IAM trust policies.
- RDS is aws_db_instance.main with manage_master_user_password = true and contains no password
  attribute.

For a platform layer, create the complete first-deployment platform and use these exact addresses:
- aws_vpc.main; public subnets aws_subnet.public_a/public_b; private subnets
  aws_subnet.private_a/private_b; internet gateway and routes.
- security groups aws_security_group.alb, aws_security_group.app, aws_security_group.db. Public
  ingress is only ALB TCP 80/443. App ingress is only from the ALB security group to web TCP 8080.
  DB ingress is only from the app security group to TCP 3306. If using separate rule resources,
  name the public HTTP rule aws_vpc_security_group_ingress_rule.alb_http. Never create or manage an
  aws_default_security_group resource.
- aws_lb.main, aws_lb_target_group.app, aws_lb_listener.http (301 HTTPS redirect),
  aws_lb_listener.https (TLS 1.2+ and forwarding).
- aws_acm_certificate.main, aws_route53_record.certificate_validation,
  aws_acm_certificate_validation.main, and aws_route53_record.app. Use the provided domain and
  hosted zone. The app record is an alias to the ALB. `for_each` is forbidden everywhere except
  aws_route53_record.certificate_validation, where it must iterate only over
  aws_acm_certificate.main.domain_validation_options.
- aws_ecs_cluster.main, aws_cloudwatch_log_group.app, aws_ecs_task_definition.app, and
  aws_ecs_service.app. The task has web and was containers named exactly `web` and `was`, placeholder
  public images, awslogs, port 8080 for web, repositoryCredentials using the Docker Hub pull secret,
  awsvpc/FARGATE, X86_64, cpu 512, memory 1024. The service starts at desired_count 0, has public IP,
  both public subnets, app security group, target group attached to web:8080, deployment circuit
  breaker with rollback, and rolling deployment percentages 100/200.
- RDS resources ending in aws_db_instance.main, using MySQL, private subnets, encrypted storage,
  manage_master_user_password=true, publicly_accessible=false and deletion_protection=true.
- aws_s3_bucket.source with versioning, encryption and public access block.
- secrets aws_secretsmanager_secret.dockerhub_push, aws_secretsmanager_secret.dockerhub_pull,
  aws_secretsmanager_secret.app_secret_key. Secret names must be exactly
  ddak-platform/dockerhub-push, ddak-platform/dockerhub-pull, and ddak/${var.project}/SECRET_KEY.
- IAM roles aws_iam_role.codebuild (name ddak-codebuild, path /ddak/pipeline/ and build boundary),
  aws_iam_role.task_execution, aws_iam_role.task, aws_iam_role.dbinit_execution (path /ddak/app/
  and app boundary), plus separate least-privilege aws_iam_role_policy resources. Trust policies
  allow only the correct AWS service, include aws:SourceAccount == ${var.account_id}, and have no
  wildcard action. Identity policy Resource values must be explicit AWS ARN strings; Secrets
  Manager ARNs may use the six-character suffix pattern ??????.
- aws_codebuild_project.main must have the exact project name `ddak-${var.project}-build`, use a
  privileged Linux container, NO_ARTIFACTS, and GITHUB source whose buildspec HCL value is exactly
  `version: 0.2\nphases:\n  build:\n    commands:\n      - exit 1\n`.
  Do not configure a `logs_config` block. Use CodeBuild's default CloudWatch Logs configuration;
  with the required project name, the default log group is
  `/aws/codebuild/ddak-${var.project}-build`, which is inside the permitted `/aws/codebuild/ddak-*`
  namespace.

Do not set environment or secrets arrays on ECS containers; the deployment layer owns runtime
configuration. Satisfy common Checkov requirements: encryption, logs, private RDS, no public S3,
restricted security groups, and least-privilege IAM. Use the exact resource labels above because
code-owned Terraform outputs bind to them.

An app-layer update is not currently accepted by this generator; do not invent cross-state
references.