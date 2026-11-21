variable "name" {
  description = "Name prefix."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "data_subnet_ids" {
  description = "Isolated data-tier subnets (one per AZ)."
  type        = list(string)
}

variable "tasks_security_group_id" {
  description = "Security group of the ECS tasks; the only source allowed to reach the data stores."
  type        = string
}

# --- RDS PostgreSQL ---------------------------------------------------------------------------------------------
variable "db_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "db_max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling."
  type        = number
  default     = 1000
}

variable "db_provisioned_iops" {
  description = "Provisioned IOPS for gp3 (only honoured when storage >= 400 GB); null keeps the gp3 baseline."
  type        = number
  default     = null
}

variable "db_multi_az" {
  type    = bool
  default = true
}

variable "db_read_replica_count" {
  description = "Optional read replicas (0 or 1 is typical). The application does not read from replicas yet."
  type        = number
  default     = 0
}

variable "db_backup_retention_days" {
  type    = number
  default = 14
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "db_commit_delay_us" {
  description = "Postgres group commit: microseconds to wait for other commits before flushing WAL (measured win in our T1/T2 runs)."
  type        = number
  default     = 200
}

variable "db_commit_siblings" {
  description = "Minimum concurrent open transactions before commit_delay applies."
  type        = number
  default     = 5
}

# --- ElastiCache Redis ------------------------------------------------------------------------------------------
variable "redis_node_type" {
  type    = string
  default = "cache.r6g.large"
}

variable "redis_num_cache_clusters" {
  description = "Primary + replicas, spread over AZs (>= 2 for Multi-AZ automatic failover)."
  type        = number
  default     = 2
}

# --- Amazon MQ for RabbitMQ -------------------------------------------------------------------------------------
variable "mq_instance_type" {
  type    = string
  default = "mq.m5.large"
}

variable "mq_engine_version" {
  type    = string
  default = "3.13"
}

variable "mq_deployment_mode" {
  description = "CLUSTER_MULTI_AZ (3 nodes, production) or SINGLE_INSTANCE (dev)."
  type        = string
  default     = "CLUSTER_MULTI_AZ"
  validation {
    condition     = contains(["CLUSTER_MULTI_AZ", "SINGLE_INSTANCE"], var.mq_deployment_mode)
    error_message = "mq_deployment_mode must be CLUSTER_MULTI_AZ or SINGLE_INSTANCE."
  }
}

# --- Secrets ----------------------------------------------------------------------------------------------------
variable "recovery_window_days" {
  description = "Secrets Manager deletion recovery window (0 = immediate, for throwaway environments)."
  type        = number
  default     = 7
}
