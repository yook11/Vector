data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # bootstrap の aws_iam_policy.*_boundary。variable にすると GitHub secret が
  # 5 本になり、1 本でも設定を忘れると plan と apply の両方が即死する
  # (image_tag と同じ詰み方)。ARN は名前から決まるので data source も要らず、
  # 「apply ロールに /vector-ci/ への iam:Get* を要求しない」既存の設計意図も保てる。
  # 名前を変えるときは bootstrap 側と両方直す (不一致なら apply が NoSuchEntity で落ちる)。
  boundary_arns = {
    for kind in [
      "frontend-task",
      "api-task",
      "scheduler-task",
      "insights-task",
      "agent-task",
      "proxy-task",
      "execution",
      "migration-task",
      "migration-execution",
      "chatbot",
      "agentcore-gateway",
    ] :
    kind => "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${var.name_prefix}-${kind}-boundary"
  }
}
