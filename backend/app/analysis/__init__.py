"""Analysis BC — AI による記事分析と埋め込みベクトル生成。

公開 API は各サブモジュール (``assessment``, ``embedding``, ``extraction``)
から直接 import する。

本 ``__init__.py`` を重い re-export ハブにすると、サブパッケージ経由で
``app.analysis`` 配下のサービス層が初期化され、``app.models.base`` との
循環 import を誘発するため、意図的に空にしている。
"""
