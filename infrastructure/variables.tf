variable "aws_region" {
  description = "AWS region for the Lambda and S3 buckets"
  type        = string
  default     = "us-east-1"
}

variable "domain" {
  description = "Full domain for the status page, e.g. status.alerce.online"
  type        = string
  default     = "status.alerce.online"
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone ID for alerce.online"
  type        = string
}

variable "environment" {
  description = "Deployment environment (staging or production)"
  type        = string
  default     = "production"
}

variable "probe_interval_minutes" {
  description = "How often the prober Lambda runs (in minutes)"
  type        = number
  default     = 1
}
