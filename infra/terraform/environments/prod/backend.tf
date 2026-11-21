# Example remote state (S3 + DynamoDB lock). Create the bucket (versioned, encrypted, private) and the lock
# table once, outside this configuration, then uncomment and adjust. The state contains generated database,
# Redis and broker passwords, so restrict access to it.
#
# terraform {
#   backend "s3" {
#     bucket         = "example-gs-alerting-tfstate"
#     key            = "prod/terraform.tfstate"
#     region         = "eu-central-1"
#     encrypt        = true
#     dynamodb_table = "example-gs-alerting-tflock"
#   }
# }
