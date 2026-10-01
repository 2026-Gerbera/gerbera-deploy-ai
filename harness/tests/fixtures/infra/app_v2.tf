# source=fixture: 실제 AWS에 적용하지 않는 C1 검증 입력.
resource "aws_secretsmanager_secret" "session" {
  name = "ddak/${var.project}/SECRET_KEY"
}

resource "aws_iam_role" "exec" {
  name                 = "ddak-${var.project}-exec"
  path                 = "/ddak/app/"
  permissions_boundary = var.app_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "read" {
  name = "session-read"
  role = aws_iam_role.exec.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = ["arn:aws:secretsmanager:ap-northeast-2:${var.account_id}:secret:ddak/${var.project}/SECRET_KEY-??????"]
    }]
  })
}
