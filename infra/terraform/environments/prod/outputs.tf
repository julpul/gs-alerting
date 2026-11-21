output "alb_dns_name" {
  description = "Point the DNS name (or the Route53 alias) at this."
  value       = module.alb.dns_name
}

output "ecr_repository_url" {
  description = "docker push target for the backend image."
  value       = module.ecr.repository_url
}

output "ecs_cluster_name" {
  value = module.ecs.cluster_name
}

output "ecs_service_names" {
  value = module.ecs.service_names
}

output "rds_endpoint" {
  value = module.data.db_endpoint
}

output "redis_primary_endpoint" {
  value = module.data.redis_primary_endpoint
}

output "rabbitmq_console_url" {
  value = module.data.mq_console_url
}
