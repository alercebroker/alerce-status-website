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
  # This is a CEILING on the sum of every per-probe timeout_s in lambda/config.json,
  # not just a per-run allowance: probes run sequentially, so a broad outage in which
  # every endpoint hangs to its own socket timeout costs their sum. Overrun it and the
  # run is killed mid-probe having written nothing -- no status.json, no uptime slot,
  # and no probe_latency logs either, since those are emitted only after every probe
  # returns. That happened: the config summed to 575 s here, and 72 of 2073 runs in the
  # week to 13/08/2026 died at the wall. The config was recalibrated from Lambda-side
  # latency logs to a 252 s budget, and test_real_config_timeout_budget_fits_the_lambda
  # _timeout reads this very number to keep the invariant enforced.
  #
  # 270 leaves ~18 s over that budget for the S3 reads/writes and the SNS publish
  # (~3.2 s measured). Do not raise it to or past 300: the schedule is every 5 minutes
  # and overlapping runs would probe the shared pgbouncer pool concurrently, which is
  # exactly what max_workers=1 exists to prevent. Adding probes eats the 29 s.
  timeout          = 270
  # Still 1024 deliberately. This was sized for the OLD per-sample history.json,
  # whose read-modify-write measured 421.8 MB of peak interpreter memory at steady
  # state (256 MB OOM'd once ~350 k entries had accumulated). uptime.json's
  # per-day-string format brings the same cycle down to 4.4 MB, so this is now
  # ~40x more than needed.
  #
  # It is NOT lowered in the same change as the format switch, on purpose: drop it
  # only after a few days of `Max Memory Used` p99 from the new format, and drop it
  # to 512 rather than 256 -- Lambda scales vCPU with memory, and the difference
  # between the two is ~$1.80/month against a function that already spends 50-240 s
  # per run. See CLAUDE.md ("Uptime history is one fixed-width string...").
  memory_size      = 1024

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
