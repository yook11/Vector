# --- A1: 収集パイプライン途絶 (供給ハートビート) ----------------------------
#
# 取得依頼の投入 (EventBridge Scheduler → source-dispatch Lambda) と受信
# (SQS → acquisition-consumer Lambda) それぞれの「正常完了した起動」が 2 時間
# ゼロなら発火する。正常時は投入 5 回/時 (HIGH 4 + MEDIUM 1)、Consumer 約 48
# 回/時。Invocations が無い時間帯はデータ点自体が無いため、TreatMissingData =
# breaching が本 alarm の核で、Scheduler 停止・受信接続の無効化・イメージ削除・
# 関数エラーのどれでも「成功が来ない」に収斂する (原因を区別しないのは意図的)。
# 関数名は文字列 local を参照し、digest 未指定で Lambda が無い構成でも plan が通る。

locals {
  lambda_success_heartbeat_alarms = {
    source_dispatch = {
      alarm_name    = "${local.source_dispatch_name}-stalled"
      function_name = local.source_dispatch_name
      description   = "取得依頼の投入 (source-dispatch Lambda) の正常完了が 2 時間ゼロ。EventBridge Scheduler の状態と /aws/lambda/${local.source_dispatch_name} のログ、失敗記録キューを確認する。"
    }
    acquisition_consumer = {
      alarm_name    = "${local.acquisition_consumer_name}-stalled"
      function_name = local.acquisition_consumer_name
      description   = "取得 Consumer (acquisition-consumer Lambda) の正常完了が 2 時間ゼロ。SQS 受信接続 (event source mapping) の状態と /aws/lambda/${local.acquisition_consumer_name} のログ、source-acquisition キューの滞留を確認する。"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_success_stalled" {
  for_each = local.lambda_success_heartbeat_alarms

  alarm_name          = each.value.alarm_name
  alarm_description   = each.value.description
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  evaluation_periods  = 2
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  dynamic "metric_query" {
    for_each = { invocations = "Invocations", errors = "Errors" }

    content {
      id = metric_query.key

      metric {
        namespace   = "AWS/Lambda"
        metric_name = metric_query.value
        period      = 3600
        stat        = "Sum"
        dimensions  = { FunctionName = each.value.function_name }
      }
    }
  }

  metric_query {
    id          = "successes"
    expression  = "FILL(invocations, 0) - FILL(errors, 0)"
    label       = "successful invocations"
    return_data = true
  }
}

# --- A4: 工程別の失敗率 -------------------------------------------------------
#
# 「仕事はしているが失敗が支配的」を工程別に検知する。シグナルは各工程の
# 分類確定点が emit する processing_outcome{stage, result} を用いる。
# AI分析失敗は原因によらず failed。
# 最小標本 10 未満の窓は IF で 0 に倒して評価しない (少量時間帯の誤発火防止)。
#
# 閾値・窓は 2026-08-12 の 28 日実測ベースライン由来の暫定値 (spec §A4)。
# embedding の窓が 12h なのは流量 (~1.2 件/h) では 3h で最小標本に届かないため。
# acquisition は対象外 (失敗の実体が特定 source の恒久ブロックで、率アラート
# に固有の守備範囲がない。source_health / A1 の担当)。completion も対象外
# (失敗の大半が外部サイトの拒否で、率が上がっても取れる対処がない)。

locals {
  # stage → 失敗率閾値・評価窓・分母の result 系列 (failed を含む)。
  pipeline_failure_rate_alarms = {
    curation = {
      threshold   = 0.5
      period      = 10800
      denominator = ["signal", "noise", "rejected", "failed"]
    }
    assessment = {
      threshold   = 0.5
      period      = 10800
      denominator = ["in_scope", "out_of_scope", "failed"]
    }
    embedding = {
      threshold   = 0.5
      period      = 43200
      denominator = ["succeeded", "failed"]
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_failure_rate" {
  for_each = local.pipeline_failure_rate_alarms

  alarm_name          = "${var.name_prefix}-failure-rate-${each.key}"
  alarm_description   = "「${each.key}」工程の失敗率が ${each.value.period / 3600} 時間窓で ${each.value.threshold * 100}% 以上 (最小標本 10)。worker ログと admin pipeline_health で error_class を確認する。"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = each.value.threshold
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  dynamic "metric_query" {
    for_each = each.value.denominator

    content {
      id = metric_query.value

      metric {
        namespace   = "Vector/Pipeline"
        metric_name = "processing_outcome"
        period      = each.value.period
        stat        = "Sum"

        dimensions = {
          stage  = each.key
          result = metric_query.value
        }
      }
    }
  }

  metric_query {
    id         = "total"
    expression = join(" + ", [for r in each.value.denominator : "FILL(${r}, 0)"])
    label      = "attempts"
  }

  metric_query {
    id          = "failure_rate"
    expression  = "IF(total >= 10, FILL(failed, 0) / total, 0)"
    label       = "failure rate"
    return_data = true
  }
}

# 停止の発生件数であり設定の修復完了は分からないため、復旧通知は送らない。
resource "aws_cloudwatch_metric_alarm" "outbox_publish_configuration_failure" {
  alarm_name          = "${var.name_prefix}-outbox-publish-configuration-failure"
  alarm_description   = "OutboxからSQSへの送信に設定修復が必要。資格情報・IAM権限・region・キュー設定を確認する。原因とevent_idは ${aws_cloudwatch_log_group.outbox_relay.name} の outbox_delivery_stopped を検索する。設定修復後の停止イベント再開は別途判断する。"
  namespace           = "Vector/Pipeline"
  metric_name         = "outbox_publish_configuration_failure"
  statistic           = "Sum"
  period              = 60
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
