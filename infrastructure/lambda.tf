# Package the Lambda from the lambda/ directory
data "archive_file" "prober" {
  type        = "zip"
  source_dir  = "${path.root}/../lambda"
  output_path = "${path.root}/../lambda.zip"
  excludes    = ["tests", "__pycache__", "*.pyc"]
}

# IAM role for the Lambda
resource "aws_iam_role" "prober" {
  name                 = "${local.name_prefix}-prober"
  permissions_boundary = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/alerce-status-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "prober_s3" {
  name = "s3-data-bucket"
  role = aws_iam_role.prober.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject"]
      # Data bucket only — prober never touches the site bucket
      Resource = "${aws_s3_bucket.data.arn}/*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "prober_logs" {
  role       = aws_iam_role.prober.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Publish component state-change alerts. Scoped to the alerts topic only; also
# gated by the alerce-status-boundary, which must allow sns:Publish on
# alerce-status-* topics for this grant to be effective.
resource "aws_iam_role_policy" "prober_sns" {
  name = "sns-publish-alerts"
  role = aws_iam_role.prober.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sns:Publish"
      Resource = aws_sns_topic.alerts.arn
    }]
  })
}

resource "aws_lambda_function" "prober" {
  function_name    = "${local.name_prefix}-prober"
  role             = aws_iam_role.prober.arn
  handler          = "prober.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.prober.output_path
  source_code_hash = data.archive_file.prober.output_base64sha256
  # Probes run sequentially and some endpoints are legitimately slow (all-catalog
  # crossmatch ~20 s, object-ranking ~13 s), so a full run can take ~2-3 min.
  # Well under the 5-min schedule, so invocations never overlap.
  timeout          = 240
  memory_size      = 256

  environment {
    variables = {
      STATUS_BUCKET   = aws_s3_bucket.data.bucket
      ALERT_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }
}

resource "aws_cloudwatch_log_group" "prober" {
  name = "/aws/lambda/${aws_lambda_function.prober.function_name}"
  # 30 days so per-probe response times (logged privately as JSON lines) can be
  # queried in Logs Insights to calibrate per-endpoint latency thresholds.
  retention_in_days = 30
}

# EventBridge schedule
resource "aws_cloudwatch_event_rule" "prober" {
  name                = "${local.name_prefix}-schedule"
  schedule_expression = "rate(${var.probe_interval_minutes} minute${var.probe_interval_minutes > 1 ? "s" : ""})"
}

resource "aws_cloudwatch_event_target" "prober" {
  rule = aws_cloudwatch_event_rule.prober.name
  arn  = aws_lambda_function.prober.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.prober.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.prober.arn
}

# Alarm: prober hasn't fired in 10 minutes. When the Lambda stops being invoked
# the Invocations metric goes MISSING (not < 1), so treat_missing_data = breaching
# is what actually makes this trip; without it the alarm never fires.
resource "aws_cloudwatch_metric_alarm" "prober_dead" {
  alarm_name          = "${local.name_prefix}-prober-dead"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 600  # 10 minutes
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "Status prober Lambda has not fired in 10 minutes"

  dimensions = {
    FunctionName = aws_lambda_function.prober.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Alarm: prober invoked but raised an error (its own job failed).
# prober_dead can't catch this — an erroring run still counts an Invocation.
resource "aws_cloudwatch_metric_alarm" "prober_errors" {
  alarm_name          = "${local.name_prefix}-prober-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_description   = "Status prober Lambda returned an error on its last run"

  dimensions = {
    FunctionName = aws_lambda_function.prober.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

output "lambda_function_name" { value = aws_lambda_function.prober.function_name }
