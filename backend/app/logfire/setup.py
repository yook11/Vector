"""Logfire (トレース・メトリクス) の bootstrap。

API / worker の各プロセス起動時に 1 度だけ呼ぶ。``logfire.configure()`` で
telemetry を初期化する。アプリのログは方針つきロガー (``app.log_policy``) が
全体設定に頼らず標準出力へ出すため、ここでは structlog を設定しない。
token 未設定の dev / CI / test では ``send_to_logfire="if-token-present"`` により
logfire は完全 no-op (外部送信なし)。

token は ``app.config.settings`` 経由で渡す。httpx instrumentation は
``AsyncClient.send`` を global patch するため、本 bootstrap で一度だけ行う。
system メトリクス (プロセス RSS / VM available) は OOM 予兆監視のため token 設定時
のみ観測する (受け手の無い env で psutil コールバックを立てない)。
"""

from __future__ import annotations

import logfire

from app.config import settings
from app.logfire.redaction import install_exception_redaction


def setup_logfire(service_name: str) -> None:
    """Logfire のトレースとメトリクスをプロセス起動時に初期化する。

    プロセスごとに 1 度だけ呼ぶ (API: lifespan startup / worker: broker の
    WORKER_STARTUP)。``logfire.configure()`` は global 可変状態のため複数回
    呼ばれても最後の設定が勝つ (冪等的)。
    """
    token = (
        settings.logfire_token.get_secret_value() if settings.logfire_token else None
    )
    logfire.configure(
        service_name=service_name,
        environment=settings.env,
        send_to_logfire="if-token-present",
        token=token,
        # 標準出力は方針つきロガーの JSON だけにする。
        console=False,
    )
    # 例外貫通で span に乗る生 str(exc) (message/stacktrace/status.description) は
    # logfire scrubber が SAFE_KEYS で素通しするため、export 前に redact する
    # (任意 PII 封じ込め。機構は redaction.py 参照)。configure 後に呼ぶ。
    install_exception_redaction()
    # httpx outbound を span 化する。prompt / AI response / 記事翻訳 /
    # Authorization header を Logfire に載せないため、capture 系 kwargs を
    # 明示的に false に固定する。
    logfire.instrument_httpx(
        capture_all=False,
        capture_headers=False,
        capture_request_body=False,
        capture_response_body=False,
    )
    # token がある (= Logfire に実送信する) 時だけ system メトリクスを観測する。
    # OOM 予兆監視に要る 2 つだけに絞り (base=None で basic の cpu/swap は出さない)、
    # 受け手の無い dev/CI/test では 60s 周期の psutil コールバックスレッドを立てない。
    # system.memory.utilization{available} = VM 逼迫判定 (Firecracker microVM なので
    # /proc/meminfo が VM 実効値を返す)。process.memory.usage = どの worker が太ったか
    # の犯人特定。
    if token is not None:
        logfire.instrument_system_metrics(
            {
                "system.memory.utilization": ["available"],
                "process.memory.usage": None,
            },
            base=None,
        )
