"""Admin API (feature-first) 領域。

管理者専用エンドポイントを feature ごとの縦スライス (router / service /
repository / schema) で構成する。``/api/v1/admin`` への集約と get_admin_user の
強制は ``app.admin.router`` が担う。
"""
