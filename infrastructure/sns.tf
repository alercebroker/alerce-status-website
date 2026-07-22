# Alerts topic — the prober publishes component state-change notifications here,
# and the Lambda self-health alarms (see lambda.tf) route here too. Delivery is
# an email subscription to a Slack channel's email-integration address, matching
# the existing ALeRCE CloudWatch-alarm -> email -> Slack pattern.
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
}

# Email subscription. Terraform can create this but cannot confirm it: SNS sends
# a confirmation link to the endpoint that must be clicked once (it arrives in
# the Slack channel). Until confirmed, publishes succeed but nothing is delivered.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# No output for the topic ARN on purpose: it embeds the account ID, and plan/apply
# output gets pasted into PRs on this public repo. Consumers reference the resource
# directly (lambda.tf).
