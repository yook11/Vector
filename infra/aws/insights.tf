# worker-insights はキャッシュ無効化のため frontend に到達する。
# ALB からの経路と同じ port に来るので、区別は application 層の
# REVALIDATE_BEARER_SECRET が担う。
#
# 通知は make_internal_async_client を使い、外向け proxy を経由しない。
resource "aws_vpc_security_group_ingress_rule" "frontend_from_insights" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "worker-insights revalidate notification"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["insights"].id
}

resource "aws_vpc_security_group_egress_rule" "insights_to_frontend" {
  security_group_id            = aws_security_group.app["insights"].id
  description                  = "revalidate notification"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}
