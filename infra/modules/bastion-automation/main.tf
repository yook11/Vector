data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}
data "aws_ami" "selected" {
  owners = ["amazon"]
  filter {
    name   = "image-id"
    values = [nonsensitive(data.aws_ssm_parameter.ami.value)]
  }
}
data "aws_network_interface" "bastion" { id = var.network.network_interface_id }

resource "aws_iam_role" "bastion" {
  name = "${var.name_prefix}-bastion"
  path = var.role_path
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_instance_profile" "bastion" {
  name = "${var.name_prefix}-bastion"
  role = aws_iam_role.bastion.name
}
resource "aws_launch_template" "bastion" {
  name                    = "${var.name_prefix}-bastion"
  image_id                = data.aws_ami.selected.id
  instance_type           = "t4g.nano"
  disable_api_termination = false
  update_default_version  = true
  iam_instance_profile { arn = aws_iam_instance_profile.bastion.arn }
  network_interfaces {
    network_interface_id  = var.network.network_interface_id
    device_index          = 0
    delete_on_termination = false
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = data.aws_ami.selected.root_device_name
    ebs {
      encrypted             = true
      delete_on_termination = true
      volume_type           = "gp3"
      volume_size           = 8
    }
  }
  dynamic "tag_specifications" {
    for_each = toset(["instance", "volume"])
    content {
      resource_type = tag_specifications.value
      tags          = local.tags
    }
  }
  lifecycle {
    precondition {
      condition = (
        data.aws_network_interface.bastion.vpc_id == var.network.vpc_id &&
        data.aws_network_interface.bastion.subnet_id == var.network.subnet_id &&
        toset(data.aws_network_interface.bastion.security_groups) == toset([var.network.security_group_id]) &&
        data.aws_ami.selected.architecture == "arm64"
      )
      error_message = "固定ENIのVPC・subnet・SGとAMIのarm64を確認してください。"
    }
  }
}
resource "aws_iam_role" "automation" {
  name = "${var.name_prefix}-bastion-automation"
  path = var.role_path
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ssm.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
        ArnLike      = { "aws:SourceArn" = "${local.ssm_arn}:automation-execution/*" }
      }
    }]
  })
}
locals {
  launch_condition = {
    ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.bastion.arn }
    Bool      = { "ec2:IsLaunchTemplateResource" = "true" }
  }
  requested_tags = { for k, v in local.tags : "aws:RequestTag/${k}" => v }
}
resource "aws_iam_role_policy" "automation" {
  name = "fixed-bastion-lifecycle"
  role = aws_iam_role.automation.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "FixedLaunchResources", Effect = "Allow", Action = "ec2:RunInstances"
        Resource = [
          "arn:aws:ec2:${local.region}::image/${data.aws_ami.selected.id}",
          "${local.ec2_arn}:network-interface/${var.network.network_interface_id}",
          aws_launch_template.bastion.arn,
        ]
        Condition = local.launch_condition
      },
      {
        Sid       = "FixedNetwork", Effect = "Allow", Action = "ec2:RunInstances"
        Resource  = ["${local.ec2_arn}:subnet/${var.network.subnet_id}", "${local.ec2_arn}:security-group/${var.network.security_group_id}"]
        Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.bastion.arn } }
      },
      {
        Sid = "FixedInstance", Effect = "Allow", Action = "ec2:RunInstances", Resource = "${local.ec2_arn}:instance/*"
        Condition = merge(local.launch_condition, {
          StringEquals = merge(local.requested_tags, {
            "ec2:InstanceType"       = "t4g.nano", "ec2:InstanceProfile" = aws_iam_instance_profile.bastion.arn,
            "ec2:MetadataHttpTokens" = "required"
          })
        })
      },
      {
        Sid = "EncryptedRoot", Effect = "Allow", Action = "ec2:RunInstances", Resource = "${local.ec2_arn}:volume/*"
        Condition = merge(local.launch_condition, {
          Bool                  = merge(local.launch_condition.Bool, { "ec2:Encrypted" = "true" })
          StringEquals          = merge(local.requested_tags, { "ec2:VolumeType" = "gp3" })
          NumericLessThanEquals = { "ec2:VolumeSize" = "8" }
        })
      },
      {
        Sid      = "TagsDuringLaunch", Effect = "Allow", Action = "ec2:CreateTags"
        Resource = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*"]
        Condition = {
          StringEquals                = merge(local.requested_tags, { "ec2:CreateAction" = "RunInstances" })
          "ForAllValues:StringEquals" = { "aws:TagKeys" = keys(local.tags) }
        }
      },
      {
        Sid       = "TerminateManaged", Effect = "Allow", Action = "ec2:TerminateInstances", Resource = "${local.ec2_arn}:instance/*"
        Condition = { StringEquals = { for k, v in local.tags : "ec2:ResourceTag/${k}" => v } }
      },
      {
        Sid       = "PassSsmInstanceRole", Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.bastion.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Sid       = "ObserveLifecycle", Effect = "Allow", Resource = "*"
        Action    = ["ec2:DescribeInstances", "ec2:DescribeNetworkInterfaces", "ec2:DescribeVolumes", "ssm:DescribeInstanceInformation", "ssm:DescribeSessions"]
        Condition = { StringEquals = { "aws:RequestedRegion" = local.region } }
      },
    ]
  })
}
