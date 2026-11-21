output "secret_arns" {
  description = "Environment variable name -> Secrets Manager ARN (injected into tasks as ECS `secrets`)."
  value = merge(
    { for k, s in aws_secretsmanager_secret.generated : k => s.arn },
    { for k, s in aws_secretsmanager_secret.external : k => s.arn },
  )
}

output "db_endpoint" {
  value = aws_db_instance.primary.address
}

output "db_replica_endpoints" {
  value = aws_db_instance.replica[*].address
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "mq_broker_name" {
  description = "CloudWatch `Broker` dimension for AWS/AmazonMQ queue metrics."
  value       = aws_mq_broker.rabbit.broker_name
}

output "mq_console_url" {
  value = aws_mq_broker.rabbit.instances[0].console_url
}
