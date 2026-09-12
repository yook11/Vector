module "runtime_ceiling" {
  source                = "../modules/runtime-policy"
  account_id            = local.account_id
  region                = local.region
  run_prefix            = "vector-test-*"
  db_resource_id        = "*"
  master_secret_arn     = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:rds!db-*"
  gemini_parameter_path = local.parameter
}

resource "aws_iam_policy" "runtime_boundary" {
  for_each = module.runtime_ceiling.policies
  name     = "vector-test-${each.key}-boundary"
  path     = local.role_path
  policy   = each.value
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role" "terraform" {
  name                 = "vector-test-terraform"
  path                 = local.role_path
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Condition = { ArnLike = {
        "aws:PrincipalArn" = var.trusted_admin_role_arn_pattern
      } }
    }]
  })
  lifecycle { prevent_destroy = true }
}

locals {
  runtime_roles    = "arn:aws:iam::${local.account_id}:role${local.runtime_path}vector-test-*"
  runtime_profiles = "arn:aws:iam::${local.account_id}:instance-profile${local.runtime_path}vector-test-*"
  smoke_tags       = { "aws:ResourceTag/Project" = "vector-test", "aws:ResourceTag/Lifecycle" = "smoke" }
  new_smoke_tags   = { "aws:RequestTag/Project" = "vector-test", "aws:RequestTag/Lifecycle" = "smoke" }
  ec2_resources    = [for kind in ["vpc", "subnet", "route-table", "internet-gateway", "security-group", "security-group-rule", "vpc-endpoint", "instance", "volume", "network-interface"] : "arn:aws:ec2:${local.region}:${local.account_id}:${kind}/*"]
}

resource "aws_iam_policy" "state" {
  name = "vector-test-terraform-state"
  path = local.role_path
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetBucketLocation", Resource = aws_s3_bucket.state.arn },
      {
        Effect    = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.state.arn
        Condition = { StringLike = { "s3:prefix" = ["smoke/*"] } }
      },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.state.arn}/smoke/*" },
      { Effect = "Allow", Action = "s3:DeleteObject", Resource = "${aws_s3_bucket.state.arn}/smoke/*.tflock" },
    ]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_policy" "runtime_iam" {
  name = "vector-test-terraform-runtime-iam"
  path = local.role_path
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole"], Resource = local.runtime_roles },
      # 削除後は元のIAMパスを解決できないため、同じ試験名の不存在確認を許可する。
      {
        Sid      = "ReadDeletedRuntimeRoles", Effect = "Allow", Action = ["iam:GetRole"]
        Resource = [for kind in ["lambda", "runner", "proxy"] : "arn:aws:iam::${local.account_id}:role/vector-test-*-${kind}"]
      },
      {
        Sid      = "ReadDeletedRuntimeProfiles", Effect = "Allow", Action = ["iam:GetInstanceProfile"]
        Resource = [for kind in ["runner", "proxy"] : "arn:aws:iam::${local.account_id}:instance-profile/vector-test-*-${kind}"]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:DeleteRole", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"]
        Resource = local.runtime_roles
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateInstanceProfile", "iam:GetInstanceProfile", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile", "iam:UntagInstanceProfile"]
        Resource = local.runtime_profiles
      },
      { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = [for p in aws_iam_policy.runtime_boundary : p.arn] },
      ], flatten([for kind, boundary in aws_iam_policy.runtime_boundary : [
        {
          Effect    = "Allow", Action = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
          Resource  = "arn:aws:iam::${local.account_id}:role${local.runtime_path}vector-test-*-${kind}"
          Condition = { ArnEquals = { "iam:PermissionsBoundary" = boundary.arn } }
        },
        {
          Effect    = "Allow", Action = "iam:PassRole"
          Resource  = "arn:aws:iam::${local.account_id}:role${local.runtime_path}vector-test-*-${kind}"
          Condition = { StringEquals = { "iam:PassedToService" = kind == "lambda" ? "lambda.amazonaws.com" : "ec2.amazonaws.com" } }
        },
    ]]))
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_policy" "network_compute" {
  name = "vector-test-terraform-network-compute"
  path = local.role_path
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ec2:Describe*"], Resource = "*" },
      {
        Sid       = "CreateTaggedNetwork", Effect = "Allow", Resource = local.ec2_resources
        Action    = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateInternetGateway", "ec2:CreateSecurityGroup", "ec2:CreateVpcEndpoint"]
        Condition = { StringEquals = merge(local.new_smoke_tags, { "aws:RequestedRegion" = local.region }), Null = { "aws:RequestTag/RunId" = "false" } }
      },
      {
        Sid       = "UseTaggedNetworkParents", Effect = "Allow"
        Action    = ["ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateVpcEndpoint"]
        Resource  = [for kind in ["vpc", "subnet", "security-group", "route-table"] : "arn:aws:ec2:${local.region}:${local.account_id}:${kind}/*"]
        Condition = { StringEquals = local.smoke_tags }
      },
      {
        Sid = "ManageTaggedResources", Effect = "Allow", Resource = local.ec2_resources
        Action = [
          "ec2:DeleteVpc", "ec2:ModifyVpcAttribute", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
          "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
          "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:ReplaceRouteTableAssociation",
          "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
          "ec2:DeleteSecurityGroup",
          "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifySecurityGroupRules",
          "ec2:DeleteVpcEndpoints", "ec2:ModifyVpcEndpoint", "ec2:TerminateInstances",
          "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions", "ec2:ModifyInstanceCreditSpecification",
          "ec2:StopInstances", "ec2:StartInstances", "ec2:DeleteVolume",
        ]
        Condition = { StringEquals = local.smoke_tags }
      },
      {
        Sid       = "LaunchTaggedCompute", Effect = "Allow", Action = "ec2:RunInstances"
        Resource  = ["arn:aws:ec2:${local.region}:${local.account_id}:instance/*", "arn:aws:ec2:${local.region}:${local.account_id}:volume/*"]
        Condition = { StringEquals = local.new_smoke_tags, Null = { "aws:RequestTag/RunId" = "false" } }
      },
      {
        Sid       = "UseTaggedNetwork", Effect = "Allow", Action = "ec2:RunInstances"
        Resource  = ["arn:aws:ec2:${local.region}:${local.account_id}:subnet/*", "arn:aws:ec2:${local.region}:${local.account_id}:security-group/*"]
        Condition = { StringEquals = local.smoke_tags }
      },
      {
        Sid      = "LaunchInterfaces", Effect = "Allow", Action = "ec2:RunInstances"
        Resource = "arn:aws:ec2:${local.region}:${local.account_id}:network-interface/*"
      },
      {
        Sid       = "AmazonLinuxImages", Effect = "Allow", Action = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:${local.region}::image/*"
        Condition = { StringEquals = { "ec2:Owner" = "amazon" } }
      },
      {
        Sid = "TagOnCreate", Effect = "Allow", Action = "ec2:CreateTags", Resource = local.ec2_resources
        Condition = {
          StringEquals = { "ec2:CreateAction" = ["CreateVpc", "CreateSubnet", "CreateRouteTable", "CreateInternetGateway", "CreateSecurityGroup", "CreateVpcEndpoint", "RunInstances", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"] }
        }
      },
      {
        Sid       = "MaintainTags", Effect = "Allow", Action = ["ec2:CreateTags", "ec2:DeleteTags"], Resource = local.ec2_resources
        Condition = { StringEquals = local.smoke_tags, "ForAllValues:StringNotEquals" = { "aws:TagKeys" = ["Project", "Lifecycle", "RunId"] } }
      },
    ]
  })
  lifecycle { prevent_destroy = true }
}


resource "aws_iam_policy" "security_group_rules" {
  name = "vector-test-terraform-security-group-rules"
  path = local.role_path
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AuthorizeOnTaggedGroups", Effect = "Allow"
        Action    = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"]
        Resource  = "arn:aws:ec2:${local.region}:${local.account_id}:security-group/*"
        Condition = { StringEquals = local.smoke_tags }
      },
      {
        Sid       = "CreateTaggedRules", Effect = "Allow"
        Action    = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"]
        Resource  = "arn:aws:ec2:${local.region}:${local.account_id}:security-group-rule/*"
        Condition = { StringEquals = local.new_smoke_tags, Null = { "aws:RequestTag/RunId" = "false" } }
      },
    ]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_policy" "services" {
  name = "vector-test-terraform-services"
  path = local.role_path
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["rds:CreateDBInstance", "rds:DeleteDBInstance", "rds:ModifyDBInstance", "rds:CreateDBSubnetGroup", "rds:DeleteDBSubnetGroup", "rds:ModifyDBSubnetGroup", "rds:CreateDBParameterGroup", "rds:DeleteDBParameterGroup", "rds:ModifyDBParameterGroup", "rds:ResetDBParameterGroup", "rds:AddTagsToResource", "rds:RemoveTagsFromResource", "rds:ListTagsForResource"]
        Resource = [for kind in ["db", "subgrp", "pg"] : "arn:aws:rds:${local.region}:${local.account_id}:${kind}:vector-test-*"]
      },
      { Effect = "Allow", Action = ["rds:Describe*"], Resource = "*" },
      {
        Effect   = "Allow", Action = ["secretsmanager:CreateSecret", "secretsmanager:TagResource", "secretsmanager:DescribeSecret"]
        Resource = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:rds!db-*"
      },
      { Effect = "Allow", Action = "kms:DescribeKey", Resource = "arn:aws:kms:${local.region}:${local.account_id}:key/*" },
      {
        Effect    = "Allow", Action = "iam:CreateServiceLinkedRole", Resource = "arn:aws:iam::${local.account_id}:role/aws-service-role/rds.amazonaws.com/AWSServiceRoleForRDS"
        Condition = { StringEquals = { "iam:AWSServiceName" = "rds.amazonaws.com" } }
      },
      {
        Effect   = "Allow", Action = ["lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConcurrency", "lambda:GetRuntimeManagementConfig", "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"]
        Resource = "arn:aws:lambda:${local.region}:${local.account_id}:function:vector-test-*-embedding"
      },
      {
        Effect = "Allow", Action = "lambda:CreateEventSourceMapping", Resource = "*"
        Condition = {
          ArnLike      = { "lambda:FunctionArn" = "arn:aws:lambda:${local.region}:${local.account_id}:function:vector-test-*-embedding" }
          StringEquals = local.new_smoke_tags
        }
      },
      {
        Effect    = "Allow", Action = ["lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"]
        Resource  = "arn:aws:lambda:${local.region}:${local.account_id}:event-source-mapping:*"
        Condition = { StringEquals = local.smoke_tags }
      },
      # 削除後のトリガー照会はResourceが*になるため、読取だけをリージョンで制限する。
      {
        Sid       = "ReadEventSourceMappings", Effect = "Allow", Action = "lambda:GetEventSourceMapping", Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.region } }
      },
      {
        Effect   = "Allow", Action = ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:ListQueueTags", "sqs:TagQueue", "sqs:UntagQueue"]
        Resource = "arn:aws:sqs:${local.region}:${local.account_id}:vector-test-*-embedding"
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:ListTagsForResource", "logs:TagResource", "logs:UntagResource", "logs:ListTagsLogGroup", "logs:TagLogGroup", "logs:UntagLogGroup"]
        Sid    = "ManageSmokeLogGroups"
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/vector-test/*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/rds/instance/vector-test-*/postgresql",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/rds/instance/vector-test-*/postgresql:*",
        ]
      },
      { Effect = "Allow", Action = "logs:DescribeLogGroups", Resource = "*" },
      {
        Effect   = "Allow", Action = ["ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetRepositoryPolicy"]
        Resource = [for repo in aws_ecr_repository.images : repo.arn]
      },
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${local.region}::parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64" },
    ]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy_attachment" "terraform" {
  for_each = {
    state    = aws_iam_policy.state.arn
    runtime  = aws_iam_policy.runtime_iam.arn
    network  = aws_iam_policy.network_compute.arn
    rules    = aws_iam_policy.security_group_rules.arn
    services = aws_iam_policy.services.arn
  }
  role       = aws_iam_role.terraform.name
  policy_arn = each.value
}
