# public hosted zone は bootstrap 側に置く。
#
# 本体スタックではなくここに置く理由: レジストラ側の NS 委任が手作業の 1 回きりで、
# 本体を destroy → apply すると zone が作り直されて **NS レコードが変わり、委任が
# 壊れる**。「CI が回す前に存在していて、ほぼ触らないもの」という bootstrap の
# 定義に合う。
#
# ドメイン自体は AWS 外のレジストラで取得済み。apply 後に出力される NS 4 本を
# レジストラの NS レコードに設定すると委任が完了する。
resource "aws_route53_zone" "this" {
  name = var.root_domain

  tags = { Name = var.root_domain }
}
