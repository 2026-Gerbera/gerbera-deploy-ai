# source=fixture; representative rolling contract, never applied.
resource "aws_lb" "main" {
  name = "ddak-${var.project}"
  internal = false
  load_balancer_type = "application"
  security_groups = [aws_security_group.alb.id]
  subnets = [aws_subnet.public_a.id, aws_subnet.public_b.id]
}
resource "aws_lb_target_group" "app" {
  name = "ddak-${var.project}-app"
  vpc_id = aws_vpc.main.id
  target_type = "ip"
  port = 8080
  protocol = "HTTP"
  deregistration_delay = 30
  health_check {
    interval = 5
    timeout = 3
    healthy_threshold = 2
    unhealthy_threshold = 2
    path = "/health/ready"
    matcher = "200"
  }
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port = 80
  protocol = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port = "443"
      protocol = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port = 443
  protocol = "HTTPS"
  ssl_policy = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn = aws_acm_certificate_validation.main.certificate_arn
  default_action {
    type = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
resource "aws_acm_certificate" "main" {
  domain_name = "app.example.com"
  validation_method = "DNS"
}
resource "aws_route53_record" "certificate_validation" {
  for_each = { for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => { name = dvo.resource_record_name, record = dvo.resource_record_value, type = dvo.resource_record_type } }
  zone_id = "Z123456"
  name = each.value.name
  type = each.value.type
  records = [each.value.record]
  ttl = 60
}
resource "aws_acm_certificate_validation" "main" {
  certificate_arn = aws_acm_certificate.main.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}
resource "aws_route53_record" "app" {
  zone_id = "Z123456"
  name = "app.example.com"
  type = "A"
  alias {
    name = aws_lb.main.dns_name
    zone_id = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
resource "aws_ecs_cluster" "main" {
  name = "ddak-${var.project}"
}
resource "aws_cloudwatch_log_group" "app" {
  name = "/aws/ecs/${var.project}"
  retention_in_days = 30
}
resource "aws_ecs_task_definition" "app" {
  family = "ddak-${var.project}"
  requires_compatibilities = ["FARGATE"]
  network_mode = "awsvpc"
  cpu = "512"
  memory = "1024"
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture = "X86_64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([
    {
      name = "web"
      image = "nginx:alpine"
      essential = true
      portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
      repositoryCredentials = { credentialsParameter = aws_secretsmanager_secret.dockerhub_pull.arn }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group = "/aws/ecs/${var.project}"
          awslogs-region = "ap-northeast-2"
          awslogs-stream-prefix = "web"
        }
      }
    },
    {
      name = "was"
      image = "python:3.13-slim"
      essential = true
      repositoryCredentials = { credentialsParameter = aws_secretsmanager_secret.dockerhub_pull.arn }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group = "/aws/ecs/${var.project}"
          awslogs-region = "ap-northeast-2"
          awslogs-stream-prefix = "was"
        }
      }
    }
  ])
}
resource "aws_ecs_service" "app" {
  name = "ddak-${var.project}"
  cluster = aws_ecs_cluster.main.arn
  task_definition = aws_ecs_task_definition.app.arn
  desired_count = 0
  launch_type = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent = 200
  deployment_circuit_breaker {
    enable = true
    rollback = true
  }
  network_configuration {
    assign_public_ip = true
    subnets = [aws_subnet.public_a.id, aws_subnet.public_b.id]
    security_groups = [aws_security_group.app.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name = "web"
    container_port = 8080
  }
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
resource "aws_db_subnet_group" "main" {
  name = "ddak-${var.project}"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}
resource "aws_db_instance" "main" {
  identifier = "ddak-${var.project}"
  engine = "mysql"
  instance_class = "db.t3.micro"
  allocated_storage = 20
  username = "ddakadmin"
  db_subnet_group_name = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  storage_encrypted = true
  manage_master_user_password = true
  publicly_accessible = false
  deletion_protection = true
}
resource "aws_secretsmanager_secret" "dockerhub_push" {
  name = "ddak-platform/dockerhub-push"
}
resource "aws_secretsmanager_secret" "dockerhub_pull" {
  name = "ddak-platform/dockerhub-pull"
}
resource "aws_secretsmanager_secret" "app_secret_key" {
  name = "ddak/${var.project}/SECRET_KEY"
}
resource "aws_secretsmanager_secret" "app_database_url" {
  name = "ddak/${var.project}/DATABASE_URL"
}
