terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "domain" {
  description = "Sending domain to verify in SES."
  type        = string
}

variable "route53_zone_id" {
  description = "Optional hosted zone; when set the DKIM CNAMEs are created automatically."
  type        = string
  default     = ""
}

variable "recovery_window_days" {
  type    = number
  default = 7
}

resource "aws_ses_domain_identity" "this" {
  domain = var.domain
}

resource "aws_ses_domain_dkim" "this" {
  domain = aws_ses_domain_identity.this.domain
}

resource "aws_route53_record" "dkim" {
  count   = var.route53_zone_id == "" ? 0 : 3
  zone_id = var.route53_zone_id
  name    = "${aws_ses_domain_dkim.this.dkim_tokens[count.index]}._domainkey.${var.domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.this.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

# SES SMTP credentials are derived from an IAM access key limited to ses:SendRawEmail.
resource "aws_iam_user" "smtp" {
  name = "${var.name}-ses-smtp"
}

resource "aws_iam_user_policy" "smtp" {
  name = "send-raw-email"
  user = aws_iam_user.smtp.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ses:SendRawEmail"]
      Resource = [aws_ses_domain_identity.this.arn]
    }]
  })
}

resource "aws_iam_access_key" "smtp" {
  user = aws_iam_user.smtp.name
}

resource "aws_secretsmanager_secret" "smtp_user" {
  name                    = "${var.name}/SMTP_USER"
  recovery_window_in_days = var.recovery_window_days
}

resource "aws_secretsmanager_secret_version" "smtp_user" {
  secret_id     = aws_secretsmanager_secret.smtp_user.id
  secret_string = aws_iam_access_key.smtp.id
}

resource "aws_secretsmanager_secret" "smtp_password" {
  name                    = "${var.name}/SMTP_PASSWORD"
  recovery_window_in_days = var.recovery_window_days
}

resource "aws_secretsmanager_secret_version" "smtp_password" {
  secret_id     = aws_secretsmanager_secret.smtp_password.id
  secret_string = aws_iam_access_key.smtp.ses_smtp_password_v4
}

output "smtp_host" {
  value = "email-smtp.${var.region}.amazonaws.com"
}

output "secret_arns" {
  description = "Environment variable name -> Secrets Manager ARN."
  value = {
    SMTP_USER     = aws_secretsmanager_secret.smtp_user.arn
    SMTP_PASSWORD = aws_secretsmanager_secret.smtp_password.arn
  }
}
