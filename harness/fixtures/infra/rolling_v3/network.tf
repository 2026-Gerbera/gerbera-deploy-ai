# source=fixture; representative rolling contract, never applied.
resource "aws_vpc" "main" {
  cidr_block = "10.40.0.0/16"
  enable_dns_support = true
  enable_dns_hostnames = true
}
resource "aws_subnet" "public_a" {
  vpc_id = aws_vpc.main.id
  cidr_block = "10.40.1.0/24"
  availability_zone = "ap-northeast-2a"
}
resource "aws_subnet" "public_b" {
  vpc_id = aws_vpc.main.id
  cidr_block = "10.40.2.0/24"
  availability_zone = "ap-northeast-2c"
}
resource "aws_subnet" "private_a" {
  vpc_id = aws_vpc.main.id
  cidr_block = "10.40.11.0/24"
  availability_zone = "ap-northeast-2a"
}
resource "aws_subnet" "private_b" {
  vpc_id = aws_vpc.main.id
  cidr_block = "10.40.12.0/24"
  availability_zone = "ap-northeast-2c"
}
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}
resource "aws_route_table_association" "public_a" {
  subnet_id = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table_association" "public_b" {
  subnet_id = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}
resource "aws_security_group" "alb" {
  name = "ddak-${var.project}-alb"
  vpc_id = aws_vpc.main.id
}
resource "aws_security_group" "app" {
  name = "ddak-${var.project}-app"
  vpc_id = aws_vpc.main.id
}
resource "aws_security_group" "db" {
  name = "ddak-${var.project}-db"
  vpc_id = aws_vpc.main.id
}
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  ip_protocol = "tcp"
  from_port = 80
  to_port = 80
  cidr_ipv4 = "0.0.0.0/0"
}
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  ip_protocol = "tcp"
  from_port = 443
  to_port = 443
  cidr_ipv4 = "0.0.0.0/0"
}
resource "aws_vpc_security_group_ingress_rule" "app_web" {
  security_group_id = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol = "tcp"
  from_port = 8080
  to_port = 8080
}
resource "aws_vpc_security_group_ingress_rule" "db_mysql" {
  security_group_id = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol = "tcp"
  from_port = 3306
  to_port = 3306
}
resource "aws_vpc_security_group_egress_rule" "alb_app" {
  security_group_id = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol = "tcp"
  from_port = 8080
  to_port = 8080
}
resource "aws_vpc_security_group_egress_rule" "app_db" {
  security_group_id = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol = "tcp"
  from_port = 3306
  to_port = 3306
}
resource "aws_vpc_security_group_egress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  ip_protocol = "tcp"
  from_port = 443
  to_port = 443
  cidr_ipv4 = "0.0.0.0/0"
}
resource "aws_vpc_security_group_egress_rule" "app_dns_tcp" {
  security_group_id = aws_security_group.app.id
  ip_protocol = "tcp"
  from_port = 53
  to_port = 53
  cidr_ipv4 = "10.40.0.2/32"
}
resource "aws_vpc_security_group_egress_rule" "app_dns_udp" {
  security_group_id = aws_security_group.app.id
  ip_protocol = "udp"
  from_port = 53
  to_port = 53
  cidr_ipv4 = "10.40.0.2/32"
}
