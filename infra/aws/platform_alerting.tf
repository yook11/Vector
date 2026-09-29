# 監視・アラート基盤 (specs/observability/cloudwatch-alerting.md の Step 1)。
#
# 通知は SNS 1 topic に集約し、Amazon Q Developer in chat applications
# (旧 AWS Chatbot。API namespace / IAM action は chatbot のまま) 経由で Slack へ流す。
# アラートは「実害が出ている・確実に出る事象」だけに張る。原因側指標
# (CPU / メモリ使用率) はアラートにしない。

# --- 通知経路 --------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

# CloudWatch alarm と EventBridge rule の発報だけを受け付ける。
# policy を自前で置くと default policy は置き換わるため、alarm 側の許可も明示する。
resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudWatchAlarmsPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
      {
        Sid       = "AllowEventBridgePublish"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:events:${var.region}:${local.account_id}:rule/${var.name_prefix}-*"
          }
        }
      },
    ]
  })
}

# Slack workspace の OAuth 認可はコンソール手動 (1 回きり)。それ以降の
# channel 設定と通知経路は本リソースで管理する。
# chatbot の API endpoint は ap-northeast-1 に存在しないため us-east-2 を明示する
# (設定は account 単位で効き、他 region の SNS topic も購読できる)。
resource "aws_chatbot_slack_channel_configuration" "alerts" {
  region = "us-east-2"

  configuration_name = "${var.name_prefix}-alerts"
  iam_role_arn       = aws_iam_role.chatbot.arn
  slack_team_id      = var.slack_team_id
  slack_channel_id   = var.slack_channel_id
  sns_topic_arns     = [aws_sns_topic.alerts.arn]

  # 未指定だと AWS managed AdministratorAccess が guardrail に適用される仕様のため、
  # 通知専用 channel として読み取りに明示的に絞る。
  guardrail_policy_arns = ["arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess"]
  logging_level         = "ERROR"
}

data "aws_iam_policy_document" "chatbot_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["chatbot.amazonaws.com"]
    }
  }
}

# channel role は alarm 通知のグラフ描画に使う読み取りだけを持つ。
resource "aws_iam_role" "chatbot" {
  name                 = "${var.name_prefix}-chatbot"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.chatbot_trust.json
  permissions_boundary = local.boundary_arns["chatbot"]
}

resource "aws_iam_role_policy" "chatbot" {
  name = "alarm-graph-read"
  role = aws_iam_role.chatbot.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchReadForAlarmRendering"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricWidgetImage",
        ]
        Resource = "*"
      },
    ]
  })
}

# --- A6: AI 利用枠の枯渇 ------------------------------------------------------
#
# 残高チャージ式運用のため、枯渇 (残高切れ・per-day quota 切れ) は運用者対応が
# 必須の事象。退避機構 (stage hold 6h) は対応時間を稼ぐだけで回復させない。
# kind × provider の全系列を FILL で 0 埋めして合算する (平常時は全系列が
# 存在しないのが正常。現状の翻訳層が生成するのは insufficient_balance×deepseek
# と usage_limit_exhausted×gemini の 2 組だが、将来の組を黙って見逃さないよう
# 4 組とも監視する)。
#
# ok_actions を意図的に付けない: metric は枯渇発生時にしか存在せず、退避機構が
# 再試行自体を止めるため、チャージしなくても alarm は OK へ戻る = OK 復帰は
# 残高回復を意味しない。対応済みかは対応した本人が知っており、復旧通知は
# 誤解を招くだけ。未チャージのまま hold 明けの再試行が再枯渇すれば OK→ALARM
# が再発し、リマインダーとして再通知される。

resource "aws_cloudwatch_metric_alarm" "ai_provider_exhausted" {
  alarm_name          = "${var.name_prefix}-ai-provider-exhausted"
  alarm_description   = "AI provider の利用枠が枯渇した。insufficient_balance (DeepSeek) は残高チャージ、usage_limit_exhausted (Gemini) は枠リセット待ちか tier 引き上げを判断する。"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]

  metric_query {
    id = "balance_deepseek"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "ai_provider_exhausted"
      period      = 900
      stat        = "Sum"

      dimensions = {
        kind     = "ai_error_insufficient_balance"
        provider = "deepseek"
      }
    }
  }

  metric_query {
    id = "balance_gemini"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "ai_provider_exhausted"
      period      = 900
      stat        = "Sum"

      dimensions = {
        kind     = "ai_error_insufficient_balance"
        provider = "gemini"
      }
    }
  }

  metric_query {
    id = "quota_deepseek"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "ai_provider_exhausted"
      period      = 900
      stat        = "Sum"

      dimensions = {
        kind     = "ai_error_usage_limit_exhausted"
        provider = "deepseek"
      }
    }
  }

  metric_query {
    id = "quota_gemini"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "ai_provider_exhausted"
      period      = 900
      stat        = "Sum"

      dimensions = {
        kind     = "ai_error_usage_limit_exhausted"
        provider = "gemini"
      }
    }
  }

  metric_query {
    id          = "exhausted_total"
    expression  = "SUM([FILL(balance_deepseek, 0), FILL(balance_gemini, 0), FILL(quota_deepseek, 0), FILL(quota_gemini, 0)])"
    label       = "ai_provider_exhausted total"
    return_data = true
  }
}

# --- A5: ECS タスク異常停止 (crash / OOM / 起動不能) ------------------------
#
# stopCode の allowlist でデプロイ由来の旧タスク停止 (ServiceSchedulerInitiated)
# を構造的に除外する。ELB ヘルスチェック失敗起因の kill も同 code のため
# ここでは拾わないが、その症状は A8 が正面から検知する (役割分担)。
#
# EventBridge の生イベントは Q Developer chat に配送されないことがあるため、
# input transformer で custom notification schema へ変換して SNS に流す。
# exitCode は TaskFailedToStart のイベントに存在しないことがあり、input_paths の
# 欠損は配送失敗になり得るため、rule を stopCode 別の 2 本に分ける。

resource "aws_cloudwatch_event_rule" "ecs_task_crashed" {
  name        = "${var.name_prefix}-ecs-task-crashed"
  description = "本 cluster の essential container 異常終了 (crash / OOM) を通知する。"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["EssentialContainerExited"]
    }
  })
}

resource "aws_cloudwatch_event_target" "ecs_task_crashed" {
  rule = aws_cloudwatch_event_rule.ecs_task_crashed.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      group    = "$.detail.group"
      exitCode = "$.detail.containers[0].exitCode"
      reason   = "$.detail.stoppedReason"
    }

    # exit code 137 = SIGKILL (OOM kill の典型値)。
    input_template = <<-EOT
      {"version":"1.0","source":"custom","content":{"textType":"client-markdown","title":"ECS task が異常停止: <group>","description":"exit code: <exitCode> (137 = OOM kill)\nstoppedReason: <reason>\n対応: 該当サービスのログを確認する。exit 137 ならメモリサイジングを見直す。"}}
    EOT
  }
}

resource "aws_cloudwatch_event_rule" "ecs_task_failed_to_start" {
  name        = "${var.name_prefix}-ecs-task-failed-to-start"
  description = "本 cluster の task 起動失敗 (image pull 失敗等) を通知する。"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["TaskFailedToStart"]
    }
  })
}

resource "aws_cloudwatch_event_target" "ecs_task_failed_to_start" {
  rule = aws_cloudwatch_event_rule.ecs_task_failed_to_start.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      group  = "$.detail.group"
      reason = "$.detail.stoppedReason"
    }

    input_template = <<-EOT
      {"version":"1.0","source":"custom","content":{"textType":"client-markdown","title":"ECS task が起動に失敗: <group>","description":"stoppedReason: <reason>\n対応: 該当サービスの task 定義と image、直近 deploy を確認する。"}}
    EOT
  }
}
