"""テスト専用の fake / stub 実装。

production 経路 (``app/``) は外部 AI provider (Gemini 等) を Pure DI で
hardcode する。CI / Schemathesis 等で外部 API を避けたいテストでは
本パッケージの stub を ``dependency_overrides`` 経由で差し替える。

stub を ``app/`` から切り出すことで、production 階層に「テスト時にしか
使われない実装」が混在しなくなり、Stage 4 Assessor / Stage 6 Extractor と
同じパッケージ規約 (production には Gemini / DeepSeek などの本番実装のみ、
stub は ``tests/``) に揃う。
"""
