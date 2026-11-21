# Horizontal scaling. Every role is stateless, so adding tasks is always safe:
#   api       ALBRequestCountPerTarget + CPU
#   worker    RabbitMQ backlog per running task (metric math) + CPU + step surge policy on events.in
#   delivery  RabbitMQ backlog per running task over deliveries.* + CPU
#   ai        CPU
#   scheduler / connector  fixed (min = max); several replicas are safe (FOR UPDATE SKIP LOCKED / competing consumers)

locals {
  alb_services   = { for k, s in var.services : k => s if s.autoscaling == "alb" }
  queue_services = { for k, s in var.services : k => s if s.autoscaling == "queue" }
  cpu_services   = { for k, s in var.services : k => s if s.autoscaling != "fixed" }
  surge_services = { for k, s in var.services : k => s if s.autoscaling == "queue" && s.surge_queue != "" }
}

resource "aws_appautoscaling_target" "role" {
  for_each           = var.services
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.role[each.key].name}"
  min_capacity       = each.value.min
  max_capacity       = each.value.max
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each           = local.cpu_services
  name               = "${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.role[each.key].service_namespace
  scalable_dimension = aws_appautoscaling_target.role[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.role[each.key].resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = each.value.cpu_target
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_policy" "alb_requests" {
  for_each           = local.alb_services
  name               = "${each.key}-alb-requests-per-target"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.role[each.key].service_namespace
  scalable_dimension = aws_appautoscaling_target.role[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.role[each.key].resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = each.value.alb_requests_per_target
    scale_out_cooldown = 30
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${var.alb_arn_suffix}/${var.target_group_arn_suffix}"
    }
  }
}

# Backlog per task = (sum of MessageCount over the role's queues) / RunningTaskCount.
# Keeps time-to-drain constant: at backlog_per_task = 5000 and ~1,000 ev/s per task, a task clears its share in ~5 s.
resource "aws_appautoscaling_policy" "queue_backlog" {
  for_each           = local.queue_services
  name               = "${each.key}-queue-backlog-per-task"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.role[each.key].service_namespace
  scalable_dimension = aws_appautoscaling_target.role[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.role[each.key].resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = each.value.backlog_per_task
    scale_out_cooldown = 30
    scale_in_cooldown  = 300

    customized_metric_specification {
      dynamic "metrics" {
        for_each = { for i, q in each.value.queue_names : tostring(i) => q }
        content {
          id          = "q${metrics.key}"
          label       = "MessageCount ${metrics.value}"
          return_data = false

          metric_stat {
            stat = "Maximum"
            metric {
              namespace   = "AWS/AmazonMQ"
              metric_name = "MessageCount"
              dimensions {
                name  = "Broker"
                value = var.mq_broker_name
              }
              dimensions {
                name  = "VirtualHost"
                value = var.mq_virtual_host
              }
              dimensions {
                name  = "Queue"
                value = metrics.value
              }
            }
          }
        }
      }

      metrics {
        id          = "running"
        label       = "RunningTaskCount"
        return_data = false

        metric_stat {
          stat = "Average"
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.this.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.role[each.key].name
            }
          }
        }
      }

      metrics {
        id          = "backlog"
        label       = "Backlog per task"
        expression  = "(${join("+", [for i, q in each.value.queue_names : "q${i}"])}) / running"
        return_data = true
      }
    }
  }
}

# Step policy for storms: target tracking reacts proportionally but gradually; when the entry queue is
# already far beyond what the fleet can drain, add capacity in big steps immediately.
resource "aws_appautoscaling_policy" "surge" {
  for_each           = local.surge_services
  name               = "${each.key}-surge-step"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.role[each.key].service_namespace
  scalable_dimension = aws_appautoscaling_target.role[each.key].scalable_dimension
  resource_id        = aws_appautoscaling_target.role[each.key].resource_id

  step_scaling_policy_configuration {
    adjustment_type          = "PercentChangeInCapacity"
    metric_aggregation_type  = "Maximum"
    cooldown                 = 120
    min_adjustment_magnitude = 1

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = each.value.surge_messages
      scaling_adjustment          = 50
    }

    step_adjustment {
      metric_interval_lower_bound = each.value.surge_messages
      scaling_adjustment          = 100
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "surge" {
  for_each            = local.surge_services
  alarm_name          = "${var.name}-${each.key}-${replace(each.value.surge_queue, ".", "-")}-surge"
  alarm_description   = "${each.value.surge_queue} backlog above ${each.value.surge_messages}: step-scale ${each.key} out"
  namespace           = "AWS/AmazonMQ"
  metric_name         = "MessageCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = each.value.surge_messages
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.surge[each.key].arn]

  dimensions = {
    Broker      = var.mq_broker_name
    VirtualHost = var.mq_virtual_host
    Queue       = each.value.surge_queue
  }
}

# Operators page on this one: anything in the dead-letter queue is a delivery that exhausted its retries.
resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "${var.name}-deliveries-dlq-not-empty"
  alarm_description   = "Messages in deliveries.dlq need a human (exhausted retries)."
  namespace           = "AWS/AmazonMQ"
  metric_name         = "MessageCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Broker      = var.mq_broker_name
    VirtualHost = var.mq_virtual_host
    Queue       = "deliveries.dlq"
  }
}
