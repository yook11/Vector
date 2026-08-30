# AgentCore Gateway。agent の外部検索が MCP tool として web search を呼ぶ入口。
#
# inbound は IAM (SigV4)。呼び出し元は同一 account の ECS task role だけなので、
# JWT authorizer が要求する IdP と discovery URL を持ち込む理由が無い。
# authorizer_type は AWS_IAM / CUSTOM_JWT の排他で、両方は持てない。
#
# target (web-search connector) はこの file に無い。provider 6.62 の
# aws_bedrockagentcore_gateway_target は connector を持たず、awscc にも
# gateway_target 自体が無い (= CloudFormation 未対応で Cloud Control も使えない)。
# API 側にはあるので scripts/create-websearch-target.sh が作る。provider が
# 対応したら import でこちらへ引き取る。
resource "aws_bedrockagentcore_gateway" "web_search" {
  name            = "${var.name_prefix}-web-search"
  description     = "MCP gateway exposing the managed web-search connector to the agent stage."
  role_arn        = aws_iam_role.agentcore_gateway.arn
  authorizer_type = "AWS_IAM"

  tags = { Name = "${var.name_prefix}-web-search" }
}

# Gateway が web-search connector を呼ぶときに assume する role。
#
# managed connector は「外部 API の契約と鍵の管理が要らない」という意味で、
# IAM が要らないという意味ではない。AWS の web-search 節が service role に
# InvokeGateway と InvokeWebSearch を要求している。空 role でも CreateGateway
# は通るが、実際の tools/call で AccessDenied になる。
resource "aws_iam_role" "agentcore_gateway" {
  name = "${var.name_prefix}-agentcore-gateway"
  # CI の apply ロールは iam:* を /vector/ path の中にしか持たない
  # (bootstrap/oidc.tf の IamWithinManagedPath)。path を省くと `/` に落ちて
  # ARN が managed_role_path_arn から外れ、CreateRole が 403 で拒否される。
  path = "/${var.name_prefix}/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
        # confused deputy 対策。この account の gateway からの assume だけ通す。
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:gateway/*"
          }
        }
      },
    ]
  })

  # ECS 系と天井を分ける。task / execution の boundary に bedrock-agentcore を
  # 足すと、gateway を呼ぶ理由の無いロールの天井まで上がってしまう。
  permissions_boundary = local.boundary_arns["agentcore-gateway"]
}

# service role の権限。ARN と action は AWS の web-search connector 節の
# ポリシー例に対応する。web-search.v1 は AWS 所有の service ARN で、
# account 部が `aws` になる (自 account ではない)。
#
# InvokeGateway を service role に持たせるのは web-search 節の指示による。
# knowledge-bases 節は「InvokeGateway は caller の権限であって execution role
# ではない」と書いており AWS の docs 内で食い違うが、使う connector 側の
# 記載に従う。
resource "aws_iam_role_policy" "agentcore_gateway" {
  name = "web-search-invoke"
  role = aws_iam_role.agentcore_gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeGateway"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:gateway/*"
      },
      {
        Sid      = "InvokeWebSearch"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeWebSearch"
        Resource = "arn:aws:bedrock-agentcore:${var.region}:aws:tool/web-search.v1"
      },
    ]
  })
}

# agent 段だけが gateway を呼ぶ。他段は外部検索を持たない。
resource "aws_iam_role_policy" "agentcore_gateway_invoke" {
  name = "agentcore-gateway-invoke"
  role = aws_iam_role.task["agent"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeWebSearchGateway"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = aws_bedrockagentcore_gateway.web_search.gateway_arn
      },
    ]
  })
}
