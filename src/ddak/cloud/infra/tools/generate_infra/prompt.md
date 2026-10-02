Create production-oriented AWS Terraform resource blocks for one ECS Fargate application in
ap-northeast-2. Return JSON only and match the supplied schema. Each value in `files` must be a
complete Terraform HCL file. The files may contain `resource` blocks only: never emit terraform,
provider, backend, variable, output, data, module, local, provisioner, or import blocks.

The code-owned framework supplies var.project, var.account_id, var.app_boundary_arn and
var.build_boundary_arn. Use only jsonencode as a function and never use heredocs. Do not put secret
values in Terraform. Do not create IAM users, access keys, passwords, tokens, or secret versions.
All names must be deterministic and scoped by var.project.

For a platform layer, create the complete first-deployment platform and use these exact addresses:
- aws_vpc.main; public subnets aws_subnet.public_a/public_b; private subnets
  aws_subnet.private_a/private_b; internet gateway and routes.
- security groups aws_security_group.alb, aws_security_group.app, aws_security_group.db. Public
  ingress is only ALB TCP 80/443. App ingress is only from the ALB security group to web TCP 8080.
  DB ingress is only from the app security group to TCP 3306.
- aws_lb.main, aws_lb_target_group.app, aws_lb_listener.http (301 HTTPS redirect),
  aws_lb_listener.https (TLS 1.2+ and forwarding).
- aws_acm_certificate.main, aws_route53_record.certificate_validation,
  aws_acm_certificate_validation.main, and aws_route53_record.app. Use the provided domain and
  hosted zone. The app record is an alias to the ALB.
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
- aws_codebuild_project.main, with privileged Linux container, NO_ARTIFACTS, and GITHUB source whose
  buildspec is exactly `version: 0.2\nphases:\n  build:\n    commands:\n      - exit 1\n`.

Do not set environment or secrets arrays on ECS containers; the deployment layer owns runtime
configuration. Satisfy common Checkov requirements: encryption, logs, private RDS, no public S3,
restricted security groups, and least-privilege IAM. Use the exact resource labels above because
code-owned Terraform outputs bind to them.

An app-layer update is not currently accepted by this generator; do not invent cross-state
references.
