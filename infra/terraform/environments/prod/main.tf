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

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "gs-alerting"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

locals {
  public_base_url = "https://${var.domain_name}"

  # Queue names declared by the application (backend/app/core/rabbit.py).
  events_queue     = "events.in"
  delivery_queues  = [for c in ["ui", "email", "sms", "phone", "webhook"] : "deliveries.${c}"]
  smtp_host        = var.enable_ses ? module.ses[0].smtp_host : var.smtp_host
  optional_secrets = var.enable_ses ? module.ses[0].secret_arns : {}

  common_environment = {
    ENVIRONMENT     = "production"
    LOG_LEVEL       = "INFO"
    PORT            = "8080"
    PUBLIC_BASE_URL = local.public_base_url
    MOCKGW_URL      = var.sms_gateway_url
    SMTP_HOST       = local.smtp_host
    SMTP_PORT       = var.smtp_port
    SMTP_FROM       = var.smtp_from
  }

  # Sizing comes from var.service_sizing; the scaling policy of each role is part of the architecture.
  services = {
    api = merge(var.service_sizing["api"], {
      autoscaling             = "alb"
      alb_requests_per_target = var.api_requests_per_target
      load_balanced           = true
      port                    = 8080
      extra_secret_names      = concat(["ANTHROPIC_API_KEY", "INBOUND_SMS_SECRET"], keys(local.optional_secrets))
    })
    worker = merge(var.service_sizing["worker"], {
      autoscaling      = "queue"
      queue_names      = [local.events_queue]
      backlog_per_task = var.worker_backlog_per_task
      surge_queue      = local.events_queue
      surge_messages   = 10 * var.worker_backlog_per_task
    })
    delivery = merge(var.service_sizing["delivery"], {
      autoscaling        = "queue"
      queue_names        = local.delivery_queues
      backlog_per_task   = var.delivery_backlog_per_task
      extra_secret_names = keys(local.optional_secrets)
    })
    ai = merge(var.service_sizing["ai"], {
      autoscaling        = "cpu"
      extra_secret_names = ["ANTHROPIC_API_KEY"]
    })
    scheduler = merge(var.service_sizing["scheduler"], {
      autoscaling = "fixed"
    })
    connector = merge(var.service_sizing["connector"], {
      autoscaling = "fixed"
    })
  }
}

module "network" {
  source = "../../modules/network"

  name               = var.name
  region             = var.region
  vpc_cidr           = var.vpc_cidr
  az_count           = 3
  single_nat_gateway = var.single_nat_gateway
  alb_ingress_cidr   = var.alb_ingress_cidr
}

module "ecr" {
  source = "../../modules/ecr"

  name = var.name
}

module "data" {
  source = "../../modules/data"

  name                    = var.name
  vpc_id                  = module.network.vpc_id
  data_subnet_ids         = module.network.data_subnet_ids
  tasks_security_group_id = module.network.tasks_security_group_id

  db_instance_class       = var.db_instance_class
  db_allocated_storage_gb = var.db_allocated_storage_gb
  db_read_replica_count   = var.db_read_replica_count
  db_deletion_protection  = var.db_deletion_protection
  redis_node_type         = var.redis_node_type
  mq_instance_type        = var.mq_instance_type
  mq_deployment_mode      = var.mq_deployment_mode
}

module "ses" {
  source = "../../modules/ses"
  count  = var.enable_ses ? 1 : 0

  name            = var.name
  region          = var.region
  domain          = var.ses_domain
  route53_zone_id = var.route53_zone_id
}

module "alb" {
  source = "../../modules/alb"

  name              = var.name
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  security_group_id = module.network.alb_security_group_id
  certificate_arn   = var.certificate_arn
  route53_zone_id   = var.route53_zone_id
  domain_name       = var.domain_name
  enable_waf        = var.enable_waf
}

module "ecs" {
  source = "../../modules/ecs"

  name             = var.name
  region           = var.region
  image            = "${module.ecr.repository_url}:${var.image_tag}"
  cpu_architecture = var.cpu_architecture

  private_subnet_ids      = module.network.private_subnet_ids
  tasks_security_group_id = module.network.tasks_security_group_id
  secret_arns             = merge(module.data.secret_arns, local.optional_secrets)
  common_environment      = local.common_environment

  target_group_arn        = module.alb.target_group_arn
  alb_arn_suffix          = module.alb.arn_suffix
  target_group_arn_suffix = module.alb.target_group_arn_suffix
  mq_broker_name          = module.data.mq_broker_name

  services = local.services
}
