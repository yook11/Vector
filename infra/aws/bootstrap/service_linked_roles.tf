# service-linked role を先に作る。
#
# ECS cluster / ALB / RDS instance / ElastiCache の初回作成時、AWS は
# iam:CreateServiceLinkedRole を暗黙に呼ぶ (AWSServiceRoleForECS など)。
# これらは /aws-service-role/ path に住むので、apply の IamWithinManagedPath
# (/vector/ のみ) では作れず、新規アカウントでは CreateCluster /
# CreateLoadBalancer / CreateDBInstance / CreateReplicationGroup が
# AccessDenied で落ちる。
#
# 代案は apply に iam:CreateServiceLinkedRole を iam:AWSServiceName 条件付きで
# 許すことだが、「apply の IAM は /vector/ の外に一切届かない」という形を崩す。
# admin が 1 回だけ動く bootstrap に置く方が筋が通る。
#
# 2026-07-28 時点でこのアカウントに存在するのは Organizations / SSO / Support /
# TrustedAdvisor の 4 つだけで、下記はいずれも未作成。
resource "aws_iam_service_linked_role" "this" {
  for_each = toset([
    "ecs.amazonaws.com",
    "elasticloadbalancing.amazonaws.com",
    "rds.amazonaws.com",
    "elasticache.amazonaws.com",
  ])

  aws_service_name = each.value
}
