# 入口は ALB + ACM + 独自ドメインの 1 点だけ。
# CloudFront は採らない (CDN の話であって入口の話ではない)。
#
# hosted zone は先に用意しておく。data source なので、無ければここで明確に失敗する。
data "aws_route53_zone" "this" {
  name         = var.root_domain
  private_zone = false
}

resource "aws_acm_certificate" "this" {
  domain_name       = var.frontend_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.this.domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      record = o.resource_record_value
      type   = o.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ALB は仕様上 2 AZ を要求する。target が 1 AZ でも動き、cross-zone 転送は無課金。
resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  subnets            = [for s in aws_subnet.public_alb : s.id]
  security_groups    = [aws_security_group.alb.id]

  drop_invalid_header_fields = true

  # frontend の CLIENT_IP_TRUST=alb-xff-last は「ALB が実測接続元を XFF 末尾に
  # 追記する」ことに全面依存する。AWS 既定と同値だが、暗黙依存にしないため pin する。
  xff_header_processing_mode = "append"

  tags = { Name = "${var.name_prefix}-alb" }
}

resource "aws_lb_target_group" "frontend" {
  name        = "${var.name_prefix}-frontend"
  port        = local.services["frontend"].port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  # root は未認証だと redirect するので、正常を誤判定しないよう
  # 未認証ログイン画面の SSR 成功を routing 条件にする。
  health_check {
    path                = "/auth/login"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # deregistration 待ちを既定 300 秒から縮める。task 1 つの構成で
  # deploy のたびに 5 分待つ理由がない。
  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

# 80 は 443 へ redirect するだけ。SG では 80 を開けていないので
# ここを有効にするなら security group にも 80 の ingress が要る。
resource "aws_lb_listener" "http_redirect" {
  count = var.enable_http_redirect ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_redirect" {
  count = var.enable_http_redirect ? 1 : 0

  security_group_id = aws_security_group.alb.id
  description       = "HTTP to HTTPS redirect"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_route53_record" "frontend" {
  zone_id = data.aws_route53_zone.this.zone_id
  name    = var.frontend_domain
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public entrypoint. The only path from the internet."
  vpc_id      = aws_vpc.main.id
}

# --- 入口 -----------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "alb_from_internet" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_frontend" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to frontend task"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_ingress_rule" "frontend_from_alb" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "ALB"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.alb.id
}

# backend は frontend 経由でしか到達できない (transport 層の defense in depth)。
# application 層の BFF_JWT_SIGNING_SECRET 検証と合わせて 2 層。
resource "aws_vpc_security_group_ingress_rule" "api_from_frontend" {
  security_group_id            = aws_security_group.app["api"].id
  description                  = "frontend BFF"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_egress_rule" "frontend_to_api" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "backend API"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app["api"].id
}

resource "aws_vpc_security_group_ingress_rule" "valkey_rl_from_frontend" {
  security_group_id            = aws_security_group.valkey_rl.id
  description                  = "frontend proxy.ts rate limit"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_egress_rule" "frontend_to_valkey_rl" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "Valkey rate limit"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.valkey_rl.id
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
