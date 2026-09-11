resource "aws_db_subnet_group" "smoke" {
  name       = local.prefix
  subnet_ids = [aws_subnet.smoke["db_a"].id, aws_subnet.smoke["db_c"].id]
  tags       = local.tags
}
resource "aws_db_parameter_group" "smoke" {
  name   = local.prefix
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  tags = local.tags
}
resource "aws_db_instance" "smoke" {
  identifier                          = local.prefix
  engine                              = "postgres"
  engine_version                      = "17"
  instance_class                      = "db.t4g.micro"
  allocated_storage                   = 20
  storage_type                        = "gp3"
  storage_encrypted                   = true
  db_name                             = "vector"
  username                            = "vector_master"
  manage_master_user_password         = true
  iam_database_authentication_enabled = true
  port                                = 5432
  availability_zone                   = "ap-northeast-1a"
  multi_az                            = false
  publicly_accessible                 = false
  db_subnet_group_name                = aws_db_subnet_group.smoke.name
  parameter_group_name                = aws_db_parameter_group.smoke.name
  vpc_security_group_ids              = [aws_security_group.smoke["db"].id]
  backup_retention_period             = 0
  delete_automated_backups            = true
  deletion_protection                 = false
  skip_final_snapshot                 = true
  apply_immediately                   = true
  performance_insights_enabled        = false
  monitoring_interval                 = 0
  tags                                = local.tags
}
