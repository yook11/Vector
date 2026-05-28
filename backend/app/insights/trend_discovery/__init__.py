"""Trend Discovery BC — rolling 7d trend discovery と weekly trends read model。

公開 API は各サブモジュール (``config``, ``domain``, ``repository``,
``application``, ``tasks``, ``cli``) から直接 import する。

本 ``__init__.py`` を重い re-export ハブにすると、
``app.insights.trend_discovery.domain.*`` をサブパッケージ経由で読むだけで
``app.insights.trend_discovery`` 配下のサービス層が初期化され、``app.models.base`` との
循環 import を誘発するため、意図的に空にしている
(``app.analysis.__init__.py`` と同じ理由)。
"""
