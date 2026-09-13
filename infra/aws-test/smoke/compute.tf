data "aws_ssm_parameter" "amazon_linux" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}
locals {
  no_proxy          = join(",", ["localhost", "127.0.0.1", "169.254.169.254", "ssm.${local.region}.amazonaws.com", "ssmmessages.${local.region}.amazonaws.com", aws_db_instance.smoke.address])
  non_public_ranges = jsondecode(file("${path.module}/../../../backend/app/http/non_public_ranges.json"))
  runner_domains = [
    "api.ecr.${local.region}.amazonaws.com",
    local.registry,
    "prod-${local.region}-starport-layer-bucket.s3.${local.region}.amazonaws.com",
    "secretsmanager.${local.region}.amazonaws.com",
    "sqs.${local.region}.amazonaws.com",
    "logs.${local.region}.amazonaws.com",
    "cdn.amazonlinux.com",
    "al2023-repos-${local.region}-de612dc2.s3.dualstack.${local.region}.amazonaws.com",
    "al2023-repos-${local.region}-de612dc2.s3.${local.region}.amazonaws.com",
  ]
  squid_template = templatefile("${path.module}/../../aws/templates/squid.conf.tftpl", {
    listen_port       = "${local.proxy_ip}:3128"
    private_v4_ranges = local.non_public_ranges.v4
    private_v6_ranges = local.non_public_ranges.v6
    clients = {
      embedding = { cidr = local.subnets.lambda.cidr, domains = ["generativelanguage.googleapis.com"], unrestricted = false }
      runner    = { cidr = local.subnets.runner.cidr, domains = local.runner_domains, unrestricted = false }
    }
  })
  # 署名付きURLやHTTPヘッダーをアクセスログへ残さない。
  squid_config = replace(local.squid_template,
    "access_log stdio:/dev/stdout combined",
    "logformat smoke %ts.%03tu %rm %>Hs\naccess_log stdio:/dev/stdout smoke"
  )
  startup = {
    proxy = templatefile("${path.module}/templates/proxy.sh.tftpl", {
      region              = local.region, registry = local.registry, image = local.images.proxy
      squid_config_base64 = base64encode(local.squid_config)
      log_group           = aws_cloudwatch_log_group.runtime["proxy"].name
    })
    runner = templatefile("${path.module}/templates/runner.sh.tftpl", {
      region = local.region, proxy_url = local.proxy_url, no_proxy = local.no_proxy
    })
  }
}
resource "aws_instance" "runtime" {
  for_each                    = toset(["proxy", "runner"])
  ami                         = nonsensitive(data.aws_ssm_parameter.amazon_linux.value)
  instance_type               = "t4g.small"
  subnet_id                   = aws_subnet.smoke[each.key].id
  private_ip                  = each.key == "proxy" ? local.proxy_ip : null
  associate_public_ip_address = each.key == "proxy"
  vpc_security_group_ids      = [aws_security_group.smoke[each.key].id]
  iam_instance_profile        = aws_iam_instance_profile.runtime[each.key].name
  user_data_base64            = base64encode(local.startup[each.key])
  user_data_replace_on_change = true
  source_dest_check           = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  credit_specification { cpu_credits = "standard" }
  root_block_device {
    volume_size           = each.key == "proxy" ? 8 : 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    # 必須タグは起動時に付くため、起動後の更新には変更可能なタグだけを渡す。
    tags = { Name = "${local.prefix}-${each.key}" }
  }
  tags = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
  depends_on = [
    aws_iam_role_policy.runtime,
    aws_route.internet,
    aws_route_table_association.smoke,
    aws_vpc_security_group_ingress_rule.private,
    aws_vpc_security_group_egress_rule.private,
    aws_vpc_security_group_egress_rule.proxy_internet,
    aws_vpc_endpoint.ssm,
    aws_vpc_endpoint.ssmmessages,
    data.aws_ecr_image.proxy,
  ]
}
