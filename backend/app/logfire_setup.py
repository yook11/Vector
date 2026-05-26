"""Logfire (可観測性) の bootstrap。

API / worker の各プロセス起動時に 1 度だけ呼ぶ。``logfire.configure()`` で
telemetry を初期化し、structlog の処理チェーンに ``StructlogProcessor`` を挿して
既存の構造化ログを Logfire に集約する。token 未設定の dev / CI / test では
``send_to_logfire="if-token-present"`` により logfire は完全 no-op (外部送信なし)。
stdout の見た目は env 別 (dev=ConsoleRenderer / prod=JSONRenderer)。

設計スタンス: pipeline_events は監査 SSoT として温存し、Logfire は追加の
telemetry 層に徹する (audit BC を Logfire に移さない)。token は
``app.config.settings`` 経由で渡し、``os.environ`` 直参照禁止 (CLAUDE.md) を維持。
"""

from __future__ import annotations

import logfire
import structlog

from app.config import settings


def setup_logfire(service_name: str) -> None:
    """Logfire と structlog の処理チェーンをプロセス起動時に初期化する。

    プロセスごとに 1 度だけ呼ぶ (API: lifespan startup / worker: broker の
    WORKER_STARTUP)。``structlog.configure()`` も ``logfire.configure()`` も
    global 可変状態のため複数回呼ばれても最後の設定が勝つ (冪等的)。
    """
    token = (
        settings.logfire_token.get_secret_value() if settings.logfire_token else None
    )
    logfire.configure(
        service_name=service_name,
        environment=settings.env,
        send_to_logfire="if-token-present",
        token=token,
        # stdout 表示は structlog renderer に一本化 (二重出力回避)。
        console=False,
    )
    is_prod = settings.env == "production"
    # 共通 processors: contextvar 統合 / level / stack info / ISO timestamp。
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    # 例外整形:
    # - prod (JSONRenderer) は ``format_exc_info`` で事前に文字列化が必要
    #   (JSON は exc_info tuple をそのままシリアライズできない)。
    # - dev (ConsoleRenderer) は renderer 内蔵の traceback 整形に委ねる。
    # ``StructlogProcessor`` は ``event_dict.get('exc_info')`` を読んで
    # Logfire のネイティブ例外表示に乗せるため、``format_exc_info`` より
    # **前** に置く必要がある。逆順にすると prod (token 有り = 見たい環境) で
    # 例外が文字列 attribute に劣化し、native スタックトレース解析に乗らない。
    exc_processors: list[structlog.types.Processor] = (
        [structlog.processors.format_exc_info] if is_prod else []
    )
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if is_prod
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            *shared,
            logfire.StructlogProcessor(),
            *exc_processors,
            renderer,
        ],
    )
