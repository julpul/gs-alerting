variable "name" {
  description = "Name prefix (cluster, log groups, roles)."
  type        = string
}

variable "region" {
  type = string
}

variable "image" {
  description = "Full image reference, e.g. <ecr-url>:<immutable-tag>. One image serves every role."
  type        = string
}

variable "cpu_architecture" {
  description = "ARM64 (Graviton, ~20% cheaper) or X86_64. Must match the platform the image was built for."
  type        = string
  default     = "ARM64"
  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "tasks_security_group_id" {
  type = string
}

variable "secret_arns" {
  description = "Environment variable name -> Secrets Manager ARN."
  type        = map(string)
}

variable "common_secret_names" {
  description = "Secrets injected into every role."
  type        = list(string)
  default     = ["DATABASE_URL", "REDIS_URL", "RABBITMQ_URL", "JWT_SECRET", "APP_SECRET"]
}

variable "common_environment" {
  description = "Plain environment variables injected into every role (PUBLIC_BASE_URL, SMTP_HOST, ...)."
  type        = map(string)
  default     = {}
}

variable "target_group_arn" {
  description = "ALB target group the load_balanced service registers with."
  type        = string
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffix" {
  type = string
}

variable "mq_broker_name" {
  description = "Amazon MQ broker name (CloudWatch `Broker` dimension) for queue-depth scaling."
  type        = string
}

variable "mq_virtual_host" {
  type    = string
  default = "/"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "services" {
  description = <<-EOT
    One entry per APP_ROLE. autoscaling is one of:
      alb    - target tracking on ALBRequestCountPerTarget + CPU (api)
      queue  - target tracking on RabbitMQ backlog per running task (metric math) + CPU (worker, delivery)
      cpu    - target tracking on CPU only (ai)
      fixed  - min = max = desired, HA only (scheduler, connector)
  EOT
  type = map(object({
    cpu                     = number
    memory                  = number
    min                     = number
    max                     = number
    autoscaling             = string
    cpu_target              = optional(number, 60)
    alb_requests_per_target = optional(number, 1000)
    queue_names             = optional(list(string), [])
    backlog_per_task        = optional(number, 5000)
    surge_queue             = optional(string, "")
    surge_messages          = optional(number, 50000)
    load_balanced           = optional(bool, false)
    port                    = optional(number, 0)
    extra_secret_names      = optional(list(string), [])
    environment             = optional(map(string), {})
  }))

  validation {
    condition     = alltrue([for s in values(var.services) : contains(["alb", "queue", "cpu", "fixed"], s.autoscaling)])
    error_message = "autoscaling must be one of alb, queue, cpu, fixed."
  }

  validation {
    condition     = alltrue([for s in values(var.services) : s.min <= s.max])
    error_message = "min must be <= max for every service."
  }

  validation {
    condition     = alltrue([for s in values(var.services) : s.autoscaling != "queue" || length(s.queue_names) > 0])
    error_message = "queue autoscaling needs at least one queue name."
  }
}
