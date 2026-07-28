resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db"
  subnet_ids = [for s in aws_subnet.data : s.id]

  tags = { Name = "${var.name_prefix}-db" }
}

# IAM DB auth は SSL 必須。PostgreSQL 15 以降は既定で 1 だが、
# 「誰かが既定を変えたら認証ごと壊れる」設定なので明示する。
resource "aws_db_parameter_group" "this" {
  name   = "${var.name_prefix}-pg${var.postgres_major_version}"
  family = "postgres${var.postgres_major_version}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name_prefix}-db"
  engine         = "postgres"
  engine_version = var.postgres_major_version
  instance_class = "db.t4g.micro"

  db_name  = var.name_prefix
  username = "${var.name_prefix}_master"

  # master password は Terraform に持たせない。AWS が Secrets Manager で管理し、
  # 既定 7 日でローテートする。
  #
  # Secrets Manager を「ローテートを代行できないのに月 $3」で却下したのは
  # 外部 API キーの話で、master password は AWS がローテートできる唯一の secret
  # なので当てはまらない。却下理由がもともと条件付きだったということ。
  #
  # 帰結: MIGRATION_DATABASE_URL を master に向けると毎週腐る。
  # migrator は専用 role を作り、master は break-glass 専用にする。
  manage_master_user_password = true

  # パスワードそのものを消す。誰が入れるかは task role の rds-db:connect が、
  # 入った後に何ができるかは migration の GRANT が決める。
  iam_database_authentication_enabled = true

  # 冗長化の水準は RDS が決める。ここを Single-AZ にした時点で全体の可用性の
  # 下限は 1 AZ なので、ECS task も Valkey も同じ AZ に置く。
  # 上げるときは一斉に上げる。
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

  # cron を避ける。trend discovery が毎日 15:05 UTC、weekly briefing が
  # 日曜 15:05 UTC (JST 月曜 00:05) に走る。
  # backup = JST 04:00-04:30 / maintenance = JST 木 05:00-06:00。
  backup_window      = "19:00-19:30"
  maintenance_window = "wed:20:00-wed:21:00"

  auto_minor_version_upgrade = true

  # 検証後に destroy する前提。定常運用に移すなら db_deletion_protection だけ
  # 反転させれば両方切り替わる。
  #
  # final_snapshot_identifier は skip_final_snapshot = true のとき無視されるが、
  # 静的に置いておかないと反転させた瞬間に destroy が
  # 「final_snapshot_identifier is required」で失敗する。
  # 「1 つ変えるだけ」を成立させるために先に書いておく。
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = !var.db_deletion_protection
  final_snapshot_identifier = "${var.name_prefix}-db-final"

  apply_immediately = var.apply_immediately

  parameter_group_name = aws_db_parameter_group.this.name

  tags = { Name = "${var.name_prefix}-db" }
}
