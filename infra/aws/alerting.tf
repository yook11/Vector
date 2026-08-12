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
  permissions_boundary = var.permissions_boundary_arn
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

# --- A1: 収集パイプライン途絶 (供給ハートビート) ----------------------------
#
# backend が dispatch task の正常完了時にだけ emit する EMF メトリクス
# dispatch_run{cadence=high} (app/queue/tasks/acquisition.py) が 2 時間
# 途絶えたら発火する。TreatMissingData = breaching が本 alarm の核で、
# scheduler / broker (Valkey) / dispatch worker / DB のどの死でも
# 「emit が来ない」に収斂する (原因を区別しないのは意図的)。
# dispatch_high は 15 分間隔なので、正常時は 1h bin に 4 打点入る。

resource "aws_cloudwatch_metric_alarm" "dispatch_run_stalled" {
  alarm_name          = "${var.name_prefix}-dispatch-run-stalled"
  alarm_description   = "収集パイプラインの供給が止まっている (dispatch_high の正常完了が 2 時間ゼロ)。ECS の scheduler / fetch サービスのログと、Valkey・DB の状態を確認する。"
  namespace           = "Vector/Pipeline"
  metric_name         = "dispatch_run"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    cadence = "high"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# --- A7: ユーザー向けエラー (ALB 5XX) --------------------------------------
#
# 低トラフィックのため率ではなく絶対数で判定する。ELB_5XX (target 到達不能)
# も合算し、frontend 全滅も同じ alarm で拾う。5XX 系メトリクスは発生時しか
# datapoint を持たないため FILL で 0 埋めして合算する。

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name_prefix}-alb-5xx"
  alarm_description   = "ユーザー向けリクエストで 5XX が発生している。frontend / api のログと直近 deploy を確認する。"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 5
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  metric_query {
    id = "target_5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      period      = 300
      stat        = "Sum"

      dimensions = {
        LoadBalancer = aws_lb.this.arn_suffix
      }
    }
  }

  metric_query {
    id = "elb_5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      period      = 300
      stat        = "Sum"

      dimensions = {
        LoadBalancer = aws_lb.this.arn_suffix
      }
    }
  }

  metric_query {
    id          = "total_5xx"
    expression  = "FILL(target_5xx, 0) + FILL(elb_5xx, 0)"
    label       = "ALB 5XX total"
    return_data = true
  }
}

# --- A8: frontend 到達不能 (UnHealthyHostCount) -----------------------------
#
# desired 1 なので unhealthy 1 = frontend 全停止。瞬断 (再起動 1 回) では
# 鳴らさないよう 5 分継続で判定する。

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_host" {
  alarm_name          = "${var.name_prefix}-alb-unhealthy-host"
  alarm_description   = "frontend の health check が 5 分連続で失敗している。frontend task の状態とログを確認する。"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.frontend.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
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
