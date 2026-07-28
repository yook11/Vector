variable "region" {
  description = "全リソースを置くリージョン。"
  type        = string
  default     = "ap-northeast-1"
}

variable "github_owner" {
  description = "GitHub の owner。OIDC の sub claim を組み立てるのに使う。"
  type        = string
}

variable "github_repo" {
  description = "GitHub の repository 名。"
  type        = string
}

variable "deploy_environment" {
  description = <<-EOT
    terraform apply を許す GitHub Environment 名。
    この Environment に required reviewer を設定することで、承認を経ない限り
    environment:<name> の sub claim を持つ token が発行されなくなる。
  EOT
  type        = string
  default     = "production"
}

variable "name_prefix" {
  description = "リソース名の接頭辞。"
  type        = string
  default     = "vector"
}

variable "root_domain" {
  description = "public hosted zone を作るドメイン (末尾ドット無し)。"
  type        = string
}
