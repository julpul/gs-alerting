variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "gs-alerting-prod"
}

variable "image_tag" {
  description = "Immutable tag of the image pushed to ECR (e.g. git SHA)."
  type        = string
}

variable "cpu_architecture" {
  description = "Must match the platform the image was built for (docker buildx --platform linux/arm64)."
  type        = string
  default     = "ARM64"
}

# --- Edge -------------------------------------------------------------------------------------------------------
variable "certificate_arn" {
  description = "ACM certificate ARN for the HTTPS listener."
  type        = string
}

variable "domain_name" {
  description = "Public FQDN (e.g. alerts.example.com); becomes PUBLIC_BASE_URL. Used for the Route53 alias when route53_zone_id is set."
  type        = string
}

variable "route53_zone_id" {
  description = "Optional hosted zone ID for the alias record (and SES DKIM records)."
  type        = string
  default     = ""
}

variable "alb_ingress_cidr" {
  type    = string
  default = "0.0.0.0/0"
}

variable "enable_waf" {
  type    = bool
  default = false
}

# --- Network ----------------------------------------------------------------------------------------------------
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "single_nat_gateway" {
  description = "false = one NAT gateway per AZ (production)."
  type        = bool
  default     = false
}

# --- Data stores ------------------------------------------------------------------------------------------------
variable "db_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "db_read_replica_count" {
  type    = number
  default = 0
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "redis_node_type" {
  type    = string
  default = "cache.r6g.large"
}

variable "mq_instance_type" {
  type    = string
  default = "mq.m5.large"
}

variable "mq_deployment_mode" {
  type    = string
  default = "CLUSTER_MULTI_AZ"
}

# --- Integrations -----------------------------------------------------------------------------------------------
variable "sms_gateway_url" {
  description = "Base URL of the SMS / phone / workflow / ticket gateway (MOCKGW_URL). In the demo this is mockgw."
  type        = string
  default     = ""
}

variable "enable_ses" {
  description = "Provision SES (domain identity + SMTP credentials) for the email channel."
  type        = bool
  default     = false
}

variable "ses_domain" {
  description = "Sending domain; required when enable_ses = true."
  type        = string
  default     = ""
}

variable "smtp_from" {
  type    = string
  default = "alerts@example.com"
}

variable "smtp_host" {
  description = "SMTP relay host when SES is disabled (ignored when enable_ses = true)."
  type        = string
  default     = ""
}

variable "smtp_port" {
  type    = string
  default = "587"
}

# --- Per-role sizing and scaling bounds ---------------------------------------------------------------------------
variable "service_sizing" {
  description = "Fargate cpu (units) / memory (MiB) and the autoscaling bounds for each role."
  type = map(object({
    cpu    = number
    memory = number
    min    = number
    max    = number
  }))
  default = {
    api       = { cpu = 1024, memory = 2048, min = 2, max = 20 }
    worker    = { cpu = 1024, memory = 2048, min = 4, max = 40 }
    delivery  = { cpu = 512, memory = 1024, min = 2, max = 20 }
    ai        = { cpu = 2048, memory = 4096, min = 1, max = 4 }
    scheduler = { cpu = 512, memory = 1024, min = 2, max = 2 }
    connector = { cpu = 512, memory = 1024, min = 1, max = 1 }
  }

  validation {
    condition     = toset(keys(var.service_sizing)) == toset(["api", "worker", "delivery", "ai", "scheduler", "connector"])
    error_message = "service_sizing must define exactly: api, worker, delivery, ai, scheduler, connector."
  }
}

variable "worker_backlog_per_task" {
  description = "Target events.in messages per running worker task (drain time ~ backlog / per-task rate)."
  type        = number
  default     = 5000
}

variable "delivery_backlog_per_task" {
  description = "Target deliveries.* messages per running delivery task."
  type        = number
  default     = 2000
}

variable "api_requests_per_target" {
  description = "Target ALB requests per api task per minute."
  type        = number
  default     = 6000
}
