# Shared security groups live here so that ALB, ECS and the data stores can reference each other without
# creating module dependency cycles. Data-store groups (in modules/data) admit only `tasks`.

resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  description = "Public ALB"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-alb" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from allowed clients"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.alb_ingress_cidr
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP (redirects to HTTPS)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = var.alb_ingress_cidr
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to api tasks"
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
  referenced_security_group_id = aws_security_group.tasks.id
}

resource "aws_security_group" "tasks" {
  name_prefix = "${var.name}-tasks-"
  description = "ECS Fargate tasks (all roles)"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-tasks" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "api traffic from the ALB"
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  description       = "Egress to data stores, AWS APIs (via NAT) and the LLM / SMS / webhook providers"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
