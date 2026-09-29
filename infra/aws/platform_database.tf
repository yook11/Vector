locals {
  db_endpoint = "${aws_db_instance.this.address}:${aws_db_instance.this.port}"

  # IAM 認証で URL に秘密が含まれないため、SSM ではなく environment に置く。
  # sslmode=require は db_ssl.py が verify-full に格上げする。
  backend_db_url = {
    for user in toset(["vector_app", "vector_collect", "vector_auth", "vector_outbox_relay", "vector_auth_rate_limit_cleanup", "vector_article_analysis", "vector_backfill", "vector_api", "vector_insights", "vector_agent"]) :
    user => "postgresql+asyncpg://${user}@${local.db_endpoint}/${aws_db_instance.this.db_name}?sslmode=require"
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db"
  subnet_ids = [for s in aws_subnet.data : s.id]

  tags = { Name = "${var.name_prefix}-db" }
}

# IAM DB 認証は SSL 必須のため、既定値でも明示する。
resource "aws_db_parameter_group" "this" {
  name   = "${var.name_prefix}-pg${var.postgres_major_version}"
  family = "postgres${var.postgres_major_version}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
    # static parameter は AWS が pending-reboot に固定するため、合わせないと毎回差分が出る。
    apply_method = "pending-reboot"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name_prefix}-db"
  engine         = "postgres"
  engine_version = var.postgres_major_version
  instance_class = "db.t4g.small"

  db_name  = var.name_prefix
  username = "${var.name_prefix}_master"

  # password は RDS が Secrets Manager で管理し 7 日ごとにローテートするため、設定に固定値で書かない。
  # master を使うのは承認付きのロール作成 (aws-db-roles.yml) と、障害時に踏み台経由で入る保守接続だけ。
  manage_master_user_password = true

  # 接続の可否は IAM の rds-db:connect、接続後にできることは migration の GRANT が決める。
  iam_database_authentication_enabled = true

  # 全体の可用性は RDS の AZ 構成が下限になるため、ECS task と Valkey も同じ AZ に置き、Multi-AZ 化は全体で揃える。
  multi_az          = false
  availability_zone = var.az_primary

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  storage_type          = "gp3"
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_encrypted     = true

  backup_retention_period = var.db_backup_retention_days
  copy_tags_to_snapshot   = true

  # 定時ジョブ (毎日 15:05 UTC) と重ならない時間帯に置く。
  backup_window      = "19:00-19:30"
  maintenance_window = "wed:20:00-wed:21:00"

  auto_minor_version_upgrade = true

  # 認証や SSL で接続前に落ちる失敗は app 側にログが出ないため、postgres のログで切り分ける。
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # db_deletion_protection だけで削除保護と final snapshot が連動して切り替わる。
  # final_snapshot_identifier は snapshot を取らない間は無視されるが、切り替え時に必須なので常に置く。
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = !var.db_deletion_protection
  final_snapshot_identifier = "${var.name_prefix}-db-final"

  apply_immediately = var.apply_immediately

  parameter_group_name = aws_db_parameter_group.this.name

  tags = { Name = "${var.name_prefix}-db" }
}

# RDS に作らせると無期限保持になるため先に作り、destroy 時に先に消えないよう instance には依存させない。
resource "aws_cloudwatch_log_group" "rds_postgresql" {
  name              = "/aws/rds/instance/${var.name_prefix}-db/postgresql"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-db-postgresql" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "RDS PostgreSQL. IAM DB auth."
  vpc_id      = aws_vpc.main.id
}

locals {
  db_roles_destinations = {
    database    = { sg = aws_security_group.rds.id, port = 5432 }
    images_logs = { sg = aws_security_group.migration_endpoints.id, port = 443 }
    secrets     = { sg = aws_security_group.db_roles_secrets.id, port = 443 }
  }
}

resource "aws_security_group" "db_roles" {
  name        = "${var.name_prefix}-db-roles"
  description = "Approved database role administration tasks."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name_prefix}-db-roles" }
}

resource "aws_security_group" "db_roles_secrets" {
  name        = "${var.name_prefix}-db-roles-secrets"
  description = "Secrets Manager endpoint for database role administration."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_endpoint" "db_roles_secrets" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.migration.id]
  security_group_ids  = [aws_security_group.db_roles_secrets.id]
  private_dns_enabled = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "secretsmanager:GetSecretValue"
      Resource  = "*"
      Condition = { ArnEquals = { "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-db-admin/${var.name_prefix}-db-roles-exec" } }
    }]
  })
  tags = { Name = "${var.name_prefix}-db-roles-secrets" }
}

resource "aws_vpc_security_group_egress_rule" "db_roles" {
  for_each                     = local.db_roles_destinations
  security_group_id            = aws_security_group.db_roles.id
  referenced_security_group_id = each.value.sg
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_ingress_rule" "from_db_roles" {
  for_each                     = local.db_roles_destinations
  security_group_id            = each.value.sg
  referenced_security_group_id = aws_security_group.db_roles.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "db_roles_s3" {
  security_group_id = aws_security_group.db_roles.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_cloudwatch_log_group" "db_roles" {
  name              = "/ecs/${var.name_prefix}-db-roles"
  retention_in_days = 30
}
