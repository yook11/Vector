locals {
  config = {
    account_id  = var.account_id, region = local.region, network = var.network, tags = local.tags,
    template_id = aws_launch_template.bastion.id, template_version = tostring(aws_launch_template.bastion.latest_version),
    profile_arn = aws_iam_instance_profile.bastion.arn, ami = data.aws_ami.selected.id
  }
  script_inputs = {
    Runtime = "python3.12", Handler = "handler", Script = file("${path.module}/lifecycle.py")
  }
}
resource "aws_ssm_document" "create" {
  name            = "${var.name_prefix}-bastion-create"
  document_type   = "Automation"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "0.3", description = "Create or reuse the fixed private bastion", assumeRole = aws_iam_role.automation.arn
    parameters    = {}
    mainSteps = [
      {
        name    = "create", action = "aws:executeScript", timeoutSeconds = 120, onFailure = "step:cleanup"
        inputs  = merge(local.script_inputs, { InputPayload = { action = "create", config = local.config, execution_id = "{{ automation:EXECUTION_ID }}" } })
        outputs = [{ Name = "InstanceId", Selector = "$.Payload.InstanceId", Type = "String" }]
      },
      {
        name = "online", action = "aws:waitForAwsResourceProperty", timeoutSeconds = 600, onFailure = "step:cleanup", isEnd = true
        inputs = {
          Service          = "ssm", Api = "DescribeInstanceInformation"
          Filters          = [{ Key = "InstanceIds", Values = ["{{ create.InstanceId }}"] }]
          PropertySelector = "$.InstanceInformationList[0].PingStatus", DesiredValues = ["Online"]
        }
      },
      {
        name    = "cleanup", action = "aws:executeScript", timeoutSeconds = 540, nextStep = "failed"
        inputs  = merge(local.script_inputs, { InputPayload = { action = "cleanup", config = local.config, execution_id = "{{ automation:EXECUTION_ID }}" } })
        outputs = [{ Name = "InstanceId", Selector = "$.Payload.InstanceId", Type = "String" }]
      },
      {
        name   = "failed", action = "aws:executeScript", isEnd = true
        inputs = { Runtime = "python3.12", Handler = "failed", Script = "def failed(events, context):\n    raise RuntimeError('CreateFailedInspectExecution')\n" }
      }
    ]
    outputs = ["create.InstanceId", "cleanup.InstanceId"]
  })
  depends_on = [aws_iam_role_policy.automation, aws_iam_role_policy_attachment.ssm]
}
resource "aws_ssm_document" "destroy" {
  name            = "${var.name_prefix}-bastion-destroy"
  document_type   = "Automation"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "0.3", description = "Destroy only the explicitly selected managed bastion", assumeRole = aws_iam_role.automation.arn
    parameters    = { InstanceId = { type = "String", allowedPattern = "^i-[0-9a-f]{17}$" } }
    mainSteps = [{
      name = "destroy", action = "aws:executeScript", timeoutSeconds = 540, isEnd = true
      inputs = merge(local.script_inputs, { InputPayload = {
        action = "destroy", config = local.config, instance_id = "{{ InstanceId }}", execution_id = "{{ automation:EXECUTION_ID }}"
      } })
      outputs = [{ Name = "InstanceId", Selector = "$.Payload.InstanceId", Type = "String" }]
    }]
    outputs = ["destroy.InstanceId"]
  })
  depends_on = [aws_iam_role_policy.automation]
}
resource "aws_iam_role_policy" "operator" {
  name = "fixed-bastion-automation"
  role = var.operations_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      for doc in [aws_ssm_document.create, aws_ssm_document.destroy] : {
        Effect    = "Allow", Action = "ssm:StartAutomationExecution", Resource = "${local.ssm_arn}:document/${doc.name}"
        Condition = { "ForAnyValue:StringEquals" = { "ssm:DocumentVersion" = [doc.latest_version] } }
      }
      ], [
      { Effect = "Allow", Action = "ssm:StartAutomationExecution", Resource = "${local.ssm_arn}:automation-execution/*" },
      {
        Effect = "Allow", Action = ["ssm:DescribeDocument", "ssm:GetDocument"], Resource = [for doc in [aws_ssm_document.create, aws_ssm_document.destroy] : "${local.ssm_arn}:document/${doc.name}"]
      },
      { Effect = "Allow", Action = "ssm:GetAutomationExecution", Resource = "${local.ssm_arn}:automation-execution/*" },
      {
        Effect    = "Allow", Action = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeInstances", "ssm:DescribeInstanceInformation"], Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.region } }
      },
      {
        Effect    = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.automation.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ssm.amazonaws.com" } }
      }
    ])
  })
}
output "configuration" {
  value = merge(local.config, {
    create_document    = aws_ssm_document.create.name, destroy_document = aws_ssm_document.destroy.name,
    create_version     = aws_ssm_document.create.latest_version, destroy_version = aws_ssm_document.destroy.latest_version,
    execution_role_arn = aws_iam_role.automation.arn
  })
}
