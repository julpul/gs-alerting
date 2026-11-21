terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

locals {
  secret_names = {
    for role, s in var.services : role => distinct(concat(var.common_secret_names, s.extra_secret_names))
  }
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  # Container Insights publishes RunningTaskCount, the denominator of our backlog-per-task scaling metric.
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "role" {
  for_each          = var.services
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

# --- IAM ------------------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role (used by the ECS agent to pull the image, write logs and resolve `secrets`):
# one per role, allowed to read only the secrets that role needs.
resource "aws_iam_role" "execution" {
  for_each           = var.services
  name               = "${var.name}-${each.key}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  for_each   = var.services
  role       = aws_iam_role.execution[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  for_each = var.services
  name     = "read-own-secrets"
  role     = aws_iam_role.execution[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for n in local.secret_names[each.key] : var.secret_arns[n]]
    }]
  })
}

# Task role (what the application code may do against AWS): the service talks only to Postgres, Redis and
# RabbitMQ over the network and to third-party HTTPS APIs, so it needs no AWS permissions at all.
resource "aws_iam_role" "task" {
  for_each           = var.services
  name               = "${var.name}-${each.key}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# --- Task definitions and services ----------------------------------------------------------------------------
resource "aws_ecs_task_definition" "role" {
  for_each                 = var.services
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = each.key
    image     = var.image
    essential = true

    portMappings = each.value.port > 0 ? [{ containerPort = each.value.port, protocol = "tcp" }] : []

    environment = [
      for k, v in merge(var.common_environment, each.value.environment, { APP_ROLE = each.key }) : { name = k, value = v }
    ]

    secrets = [for n in local.secret_names[each.key] : { name = n, valueFrom = var.secret_arns[n] }]

    # Fargate allows at most 120 s: the roles finish in-flight messages on SIGTERM before exiting.
    stopTimeout            = 120
    readonlyRootFilesystem = false

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.role[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_ecs_service" "role" {
  for_each        = var.services
  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.role[each.key].arn
  desired_count   = each.value.min
  launch_type     = "FARGATE"

  # Roll out new task definitions without dropping capacity; roll back automatically if tasks keep failing.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = each.value.load_balanced ? 90 : null
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.load_balanced ? [1] : []
    content {
      target_group_arn = var.target_group_arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  # Application Auto Scaling owns the running count after creation.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_ecs_cluster_capacity_providers.this]
}
