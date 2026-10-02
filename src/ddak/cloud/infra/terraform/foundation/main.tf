# 코드 소유 기반. AI 생성 번들 밖에서 제품 infra 승인 뒤 코드가 적용한다.
# AI 개발 에이전트의 Terraform 직접 실행 금지와 제품 승인 실행은 구분한다.
# bucket 미존재: 승인된 local plan → SDK bucket 생성 → platform local apply → remote backend state 이전.
# 실제 생성기 O2 연결·AWS 전체 검증은 아직 미완이다.
terraform {
  required_version = ">= 1.11, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.66" }
  }
}
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = [var.account_id]
  default_tags { tags = { ManagedBy = "ddak", Project = var.project } }
}
resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
resource "aws_iam_policy" "app_boundary" {
  name = "ddak-app-boundary"
  path = "/ddak/boundary/"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue",
      Resource = "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak-platform/dockerhub-pull-??????" },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = "arn:aws:logs:ap-northeast-2:${var.account_id}:log-group:/ecs/ddak-*:*" },
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue",
        Resource = ["arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak/*",
      "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:rds!*"] },
      { Effect = "Deny", Action = ["iam:*", "organizations:*", "account:*", "sts:AssumeRole"], Resource = "*" }
    ]
  })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_policy" "pipeline_boundary" {
  name = "ddak-build-boundary"
  path = "/ddak/boundary/"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue",
      Resource = "arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak-platform/dockerhub-push-??????" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = "arn:aws:logs:ap-northeast-2:${var.account_id}:log-group:/aws/codebuild/ddak-*:*" }
    ]
  })
  lifecycle { prevent_destroy = true }
}
