# 本体スタックの state 置き場。ここだけは local state で先に作る。
# 名前は account ID から導出して globally unique を担保する
# (variable にすると tfvars に account ID を書くことになるため)。
locals {
  state_bucket_name = "${var.name_prefix}-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.state_bucket_name

  tags = { Name = local.state_bucket_name }
}

# state は「壊した後に戻る」ための唯一の手段なので versioning は必須。
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-KMS に上げてはいけない。CI ロール 3 つには kms:Decrypt の Deny が入って
# おり (oidc.tf の secret_read_actions)、SSE-KMS は AWS 管理キーでも読み出し側に
# kms:Decrypt を要求するため、state の読み取りごと壊れて plan も apply も落ちる。
# 「暗号化を強化した」つもりの変更で CI が全滅する結合なので、ここで固定する。
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# state には secret の実体を置かない方針だが、リソース ID や ARN は全部入る。
# 平文 HTTP での取得を拒否する。
resource "aws_s3_bucket_policy" "tfstate_tls_only" {
  bucket = aws_s3_bucket.tfstate.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tfstate.arn,
          "${aws_s3_bucket.tfstate.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}
