"""Analysis BC — AI による記事分析と埋め込みベクトル生成。

公開 API は各サブモジュール (``errors``, ``classification_service``,
``embedding_service``, ``classifier``, ``embedder``, ``extraction``,
``domain``) から直接 import する。

本 ``__init__.py`` を重い re-export ハブにすると、
``app.analysis.domain.value_objects.*`` をサブパッケージ経由で読むだけで
``app.analysis`` 配下のサービス層（classification_service 等）が初期化され、
``app.models.base`` との循環 import を誘発するため、意図的に空にしている。
"""
