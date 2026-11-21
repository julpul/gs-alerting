terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# --- Security groups: only the ECS tasks reach the data stores ------------------------------------------------
resource "aws_security_group" "rds" {
  name_prefix = "${var.name}-rds-"
  description = "PostgreSQL, tasks only"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.name}-redis-"
  description = "Redis, tasks only"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "mq" {
  name_prefix = "${var.name}-mq-"
  description = "RabbitMQ AMQPS, tasks only"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_tasks" {
  security_group_id            = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = var.tasks_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_tasks" {
  security_group_id            = aws_security_group.redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = var.tasks_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "mq_from_tasks" {
  security_group_id            = aws_security_group.mq.id
  ip_protocol                  = "tcp"
  from_port                    = 5671
  to_port                      = 5671
  referenced_security_group_id = var.tasks_security_group_id
}

# --- Generated credentials (stored in Secrets Manager, never in tfvars) ---------------------------------------
# NOTE: random_password values live in the Terraform state; use an encrypted, access-controlled state bucket.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "redis" {
  length  = 32
  special = false
}

resource "random_password" "mq" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "app" {
  length  = 64
  special = false
}

# --- RDS PostgreSQL 16 (pgvector is available on RDS; the app runs CREATE EXTENSION vector at start) -----------
resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.data_subnet_ids
}

resource "aws_db_parameter_group" "pg16" {
  name_prefix = "${var.name}-pg16-"
  family      = "postgres16"
  description = "gs-alerting tuning: WAL group commit, TLS only"

  # Static parameter (needs a reboot on first apply). pgvector itself needs no preload.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  # Group commit: batches fsyncs under concurrent worker commits without giving up durability.
  parameter {
    name  = "commit_delay"
    value = tostring(var.db_commit_delay_us)
  }

  parameter {
    name  = "commit_siblings"
    value = tostring(var.db_commit_siblings)
  }

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "primary" {
  identifier     = "${var.name}-pg"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name  = "alerting"
  username = "alerting"
  password = random_password.db.result
  port     = 5432

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_type          = "gp3"
  iops                  = var.db_provisioned_iops
  storage_encrypted     = true

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.pg16.name

  backup_retention_period      = var.db_backup_retention_days
  backup_window                = "02:00-03:00"
  maintenance_window           = "sun:03:30-sun:04:30"
  copy_tags_to_snapshot        = true
  auto_minor_version_upgrade   = true
  deletion_protection          = var.db_deletion_protection
  skip_final_snapshot          = !var.db_deletion_protection
  final_snapshot_identifier    = var.db_deletion_protection ? "${var.name}-pg-final" : null
  performance_insights_enabled = true
  enabled_cloudwatch_logs_exports = [
    "postgresql",
    "upgrade",
  ]
}

resource "aws_db_instance" "replica" {
  count               = var.db_read_replica_count
  identifier          = "${var.name}-pg-replica-${count.index}"
  replicate_source_db = aws_db_instance.primary.identifier
  instance_class      = var.db_instance_class
  storage_encrypted   = true

  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  publicly_accessible    = false

  auto_minor_version_upgrade   = true
  performance_insights_enabled = true
  skip_final_snapshot          = true
}

# --- ElastiCache Redis (replication group, Multi-AZ, TLS + AUTH) ----------------------------------------------
resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-redis"
  subnet_ids = var.data_subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.name}-redis"
  description          = "gs-alerting: rule reload pub/sub, idempotency, rate limits, SSE fan-out"

  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.redis_num_cache_clusters
  port                       = 6379
  automatic_failover_enabled = var.redis_num_cache_clusters > 1
  multi_az_enabled           = var.redis_num_cache_clusters > 1

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result

  snapshot_retention_limit   = 3
  snapshot_window            = "01:00-02:00"
  maintenance_window         = "sun:02:30-sun:03:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false
}

# --- Amazon MQ for RabbitMQ (3-node cluster across AZs) -------------------------------------------------------
resource "aws_mq_broker" "rabbit" {
  broker_name                = "${var.name}-rabbit"
  engine_type                = "RabbitMQ"
  engine_version             = var.mq_engine_version
  host_instance_type         = var.mq_instance_type
  deployment_mode            = var.mq_deployment_mode
  auto_minor_version_upgrade = true
  publicly_accessible        = false
  security_groups            = [aws_security_group.mq.id]
  # Cluster brokers are placed across AZs by the service behind one NLB endpoint; a private broker is
  # attached to a single subnet of the VPC.
  subnet_ids = slice(var.data_subnet_ids, 0, 1)

  user {
    username = "alerting"
    password = random_password.mq.result
  }

  logs {
    general = true
  }

  maintenance_window_start_time {
    day_of_week = "SUNDAY"
    time_of_day = "04:30"
    time_zone   = "UTC"
  }
}

# --- Secrets Manager: everything the tasks receive via `secrets` ----------------------------------------------
locals {
  # amqps://b-xxxx.mq.<region>.amazonaws.com:5671 -> host:port
  mq_hostport = trimprefix(aws_mq_broker.rabbit.instances[0].endpoints[0], "amqps://")

  generated_secrets = {
    DATABASE_URL = "postgresql://alerting:${random_password.db.result}@${aws_db_instance.primary.address}:5432/alerting?sslmode=require"
    REDIS_URL    = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
    RABBITMQ_URL = "amqps://alerting:${random_password.mq.result}@${local.mq_hostport}/"
    JWT_SECRET   = random_password.jwt.result
    APP_SECRET   = random_password.app.result
  }

  # Provider API keys are supplied out of band (`aws secretsmanager put-secret-value`), never through Terraform.
  external_secrets = toset(["ANTHROPIC_API_KEY", "INBOUND_SMS_SECRET"])
}

resource "aws_secretsmanager_secret" "generated" {
  for_each                = local.generated_secrets
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = var.recovery_window_days
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = local.generated_secrets
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = each.value
}

resource "aws_secretsmanager_secret" "external" {
  for_each                = local.external_secrets
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = var.recovery_window_days
}

# ECS refuses to start a task whose secret has no value; seed a placeholder that the operator overwrites.
resource "aws_secretsmanager_secret_version" "external" {
  for_each      = local.external_secrets
  secret_id     = aws_secretsmanager_secret.external[each.key].id
  secret_string = "REPLACE_ME"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
