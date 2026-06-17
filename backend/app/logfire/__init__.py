"""Logfire (可観測性) 関連の bootstrap / helper を束ねる package。

submodule:
- ``setup`` — プロセス起動時の ``logfire.configure()`` + structlog 配線。
- ``db_pool`` — DB コネクションプールの起動ログと metrics 登録。
- ``exceptions`` — PII-safe な ``__str__`` を持つドメイン例外基底。
- ``article_stage`` — AI 分析パイプラインの記事ステージ span helper。
- ``stage_span`` — 非 AI worker 工程の汎用ステージ span helper。

re-export はしない。利用側は submodule をフルパスで import する
(``from app.logfire.setup import setup_logfire`` 等)。``import logfire`` は
絶対 import のため、この package が pip の ``logfire`` を shadow することはない。
"""

from __future__ import annotations
