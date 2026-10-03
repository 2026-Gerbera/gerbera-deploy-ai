# source=fixture; representative rolling contract, never applied.
resource "aws_iam_role" "codebuild" {
  name = "ddak-codebuild"
  path = "/ddak/pipeline/"
  permissions_boundary = var.build_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = { Service = "codebuild.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = "${var.account_id}" } }
    }]
  })
}
resource "aws_iam_role" "task_execution" {
  name = "ddak-${var.project}-exec"
  path = "/ddak/app/"
  permissions_boundary = var.app_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = "${var.account_id}" } }
    }]
  })
}
resource "aws_iam_role" "task" {
  name = "ddak-${var.project}-task"
  path = "/ddak/app/"
  permissions_boundary = var.app_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = "${var.account_id}" } }
    }]
  })
}
resource "aws_iam_role" "dbinit_execution" {
  name = "ddak-${var.project}-dbinit-exec"
  path = "/ddak/app/"
  permissions_boundary = var.app_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = "${var.account_id}" } }
    }]
  })
}
resource "aws_iam_role_policy" "task_execution" {
  role = aws_iam_role.task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:ap-northeast-2:${var.account_id}:log-group:/aws/ecs/${var.project}:*"
      },
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak-platform/dockerhub-pull-??????",
          "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak/${var.project}/SECRET_KEY-??????",
          "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak/${var.project}/DATABASE_URL-??????"
        ]
      }
    ]
  })
}
resource "aws_iam_role_policy" "codebuild" {
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:ap-northeast-2:${var.account_id}:log-group:/aws/codebuild/ddak-${var.project}-build:*"
      },
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak-platform/dockerhub-push-??????"
      }
    ]
  })
}
resource "aws_s3_bucket" "source" {
  bucket = "ddak-${var.project}-${var.account_id}-source"
}
resource "aws_s3_bucket_versioning" "source" {
  bucket = aws_s3_bucket.source.id
  versioning_configuration {
    status = "Enabled"
  }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  bucket = aws_s3_bucket.source.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_public_access_block" "source" {
  bucket = aws_s3_bucket.source.id
  block_public_acls = true
  block_public_policy = true
  ignore_public_acls = true
  restrict_public_buckets = true
}
resource "aws_codebuild_project" "main" {
  name = "ddak-${var.project}-build"
  service_role = aws_iam_role.codebuild.arn
  artifacts {
    type = "NO_ARTIFACTS"
  }
  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image = "aws/codebuild/standard:7.0"
    type = "LINUX_CONTAINER"
    privileged_mode = true
  }
  source {
    type = "GITHUB"
    location = "https://github.com/example/app"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - exit 1\n"
  }
}
