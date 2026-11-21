variable "name" {
  description = "Name prefix for all network resources."
  type        = string
}

variable "region" {
  description = "AWS region (used for the S3 gateway endpoint service name)."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR, must be a /16 (carved into /20 subnets)."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to span (1-3)."
  type        = number
  default     = 3
  validation {
    condition     = var.az_count >= 1 && var.az_count <= 3
    error_message = "az_count must be between 1 and 3."
  }
}

variable "api_port" {
  description = "Container port of the api role."
  type        = number
  default     = 8080
}

variable "alb_ingress_cidr" {
  description = "CIDR allowed to reach the public ALB."
  type        = string
  default     = "0.0.0.0/0"
}

variable "single_nat_gateway" {
  description = "Use one shared NAT gateway (cheaper, AZ-level SPOF for egress) instead of one per AZ."
  type        = bool
  default     = false
}
