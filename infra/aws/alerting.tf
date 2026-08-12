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

# --- A3: 観測の死活 (メタ監視) ---------------------------------------------
#
# 毎分の queue_health 観測 (app/queue/tasks/queue_health.py) が stage 別に
# emit する observation_up{stage} の全系列を監視する。A2 (工程別滞留) は
# この観測に全面依存するため、先に「監視の目が開いているか」を塞ぐ。
#
# FILL で欠損を 0 とみなしてから MIN を取ることで、
#   値 0 (Valkey / stream の snapshot 失敗) / 特定 stage だけの emit 消失
#   (観測 task の途中 crash) / 全欠損 (maintenance worker・scheduler 死)
# の 3 形態が同じ式で落ちる。デプロイ時の 1〜2 分の空白は 5 分連続条件が吸収。

resource "aws_cloudwatch_metric_alarm" "queue_observation_stalled" {
  alarm_name          = "${var.name_prefix}-queue-observation-stalled"
  alarm_description   = "パイプライン監視の観測が 5 分間できていない (queue_health)。全 stage 同時なら Valkey か maintenance worker (analysis サービス内) を、単一 stage ならその stream を確認する。"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  metric_query {
    id = "obs_acquisition"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "observation_up"
      period      = 60
      stat        = "Minimum"

      dimensions = {
        stage = "acquisition"
      }
    }
  }

  metric_query {
    id = "obs_completion"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "observation_up"
      period      = 60
      stat        = "Minimum"

      dimensions = {
        stage = "completion"
      }
    }
  }

  metric_query {
    id = "obs_curation"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "observation_up"
      period      = 60
      stat        = "Minimum"

      dimensions = {
        stage = "curation"
      }
    }
  }

  metric_query {
    id = "obs_assessment"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "observation_up"
      period      = 60
      stat        = "Minimum"

      dimensions = {
        stage = "assessment"
      }
    }
  }

  metric_query {
    id = "obs_embedding"

    metric {
      namespace   = "Vector/Pipeline"
      metric_name = "observation_up"
      period      = 60
      stat        = "Minimum"

      dimensions = {
        stage = "embedding"
      }
    }
  }

  metric_query {
    id          = "observation_floor"
    expression  = "MIN([FILL(obs_acquisition, 0), FILL(obs_completion, 0), FILL(obs_curation, 0), FILL(obs_assessment, 0), FILL(obs_embedding, 0)])"
    label       = "observation_up floor"
    return_data = true
  }
}

# --- A2: 工程別滞留 -----------------------------------------------------------
#
# 「その工程に仕事が積まれたまま閾値以上消化されていない」を stage 名指しで
# 検知する。シグナルは queue_health が毎分 emit する最古の未処理 entry の
# 経過秒数。仕事ゼロのときは 0 が emit されるため、量に依存せず
# 「暇」と「死んでいる」を誤判定しない。missing = notBreaching は
# 観測死を A3 に委ねる役割分担 (生きていれば正常時に missing は発生しない)。
# AI 工程の閾値が緩いのは、AI 予算枯渇などの際に再試行を後ろへずらして
# 対応時間 (残高チャージ等) を稼ぐ退避機構が働くため。60 分は「退避が
# 猶予を稼いでいる想定内の遅延」と「対応が間に合っていない」の境界であり、
# 初期値として置き実測で調整する。

locals {
  # stage → 滞留閾値 (秒) と、対応時に見に行く ECS サービス。
  pipeline_stall_alarms = {
    acquisition = { threshold = 1800, service = "fetch" }
    completion  = { threshold = 1800, service = "fetch" }
    curation    = { threshold = 3600, service = "analysis" }
    assessment  = { threshold = 3600, service = "analysis" }
    embedding   = { threshold = 3600, service = "analysis" }
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_stage_stalled" {
  for_each = local.pipeline_stall_alarms

  alarm_name          = "${var.name_prefix}-pipeline-stalled-${each.key}"
  alarm_description   = "「${each.key}」工程に仕事が積まれたまま ${each.value.threshold / 60} 分以上消化されていない。ECS の ${each.value.service} サービスの worker ログと queue の状態を確認する。"
  namespace           = "Vector/Pipeline"
  metric_name         = "oldest_outstanding_enqueue_age"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    stage = each.key
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# --- A4: 工程別の失敗率 -------------------------------------------------------
#
# 「仕事はしているが失敗が支配的」を工程別に検知する。シグナルは各工程の
# 分類確定点が emit する processing_outcome{stage, result}。分母は各工程の
# 既存不変条件を踏襲し infra_error は分母外。最小標本 10 未満の窓は IF で
# 0 に倒して評価しない (少量時間帯の誤発火防止)。
#
# 閾値・窓は 2026-08-12 の 28 日実測ベースライン由来の暫定値 (spec §A4)。
# completion の 90% は「慢性 54% 失敗 (外部サイトのブロック) が普段の姿」の
# 上に置いた「ほぼ全滅 = scraper/egress の構造故障」の線。embedding の窓が
# 12h なのは流量 (~1.2 件/h) では 3h で最小標本に届かないため。
# acquisition は対象外 (失敗の実体が特定 source の恒久ブロックで、率アラート
# に固有の守備範囲がない。source_health / A1 の担当)。

locals {
  # stage → 失敗率閾値・評価窓・分母の result 系列 (failed を含む)。
  pipeline_failure_rate_alarms = {
    completion = {
      threshold   = 0.9
      period      = 10800
      denominator = ["succeeded", "failed"]
    }
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
