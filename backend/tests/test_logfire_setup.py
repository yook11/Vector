"""``setup_logfire`` の不変条件テスト。

検証する性質:
- token 未設定 (dev/CI/test) では ``logfire.configure`` に ``token=None`` と
  ``send_to_logfire="if-token-present"`` が渡され、外部送信が **絶対に** 起きない。
- token 設定時は ``SecretStr.get_secret_value()`` の生値が渡る (Settings 経由
  での秘密保管を維持しつつ logfire SDK へは生値が必要)。
- ``settings.env`` で stdout renderer を切り替える (prod=JSON / dev=Console)。
- ``StructlogProcessor`` は **必ず** ``format_exc_info`` より前に置かれる
  (逆順だと prod で例外が文字列 attribute に劣化し native スタックトレース
  解析に乗らない / feedback_failure_visibility)。
- bootstrap 後に ``structlog.get_logger(...).info(...)`` が例外を投げない
  (processor チェーンとして実際に成立する)。

注意: ``structlog.configure()`` / ``logfire.configure()`` は global 可変状態
なので、本テストはどちらも patch して processor 列の検査で完結させ、他テスト
への state 汚染を避ける (plan §テスト方針)。

検証は **identity / isinstance** で行う (名前 string 一致ではなく):
- ``structlog.processors.format_exc_info`` は singleton インスタンス (25.x で
  ``ExceptionRenderer`` の instance) なので identity 検査が正本契約。
- ``logfire.StructlogProcessor`` は instance ごとに別個なので isinstance 検査。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import logfire
import pytest
import structlog
from logfire.integrations.structlog import LogfireProcessor
from pydantic import SecretStr

from app.logfire import setup as logfire_setup_module
from app.logfire.setup import setup_logfire


@pytest.fixture
def patched_configures(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """``logfire.configure`` / ``structlog.configure`` / ``logfire.instrument_httpx``
    / ``logfire.instrument_system_metrics`` を patch して呼出を捕捉。

    Global 可変状態への副作用を完全に遮断するため 4 つすべて MagicMock に差し
    替える。``instrument_httpx`` は ``AsyncClient.send`` の module-level patch
    なので、patch せずに本物を呼ぶと httpx の通常使用が壊れる (テスト並走時)。
    ``instrument_system_metrics`` も patch しないと token 設定時に実 OTel 収集器
    (60s 周期 psutil コールバックスレッド) がテスト内で起動してしまう。
    """
    mock_logfire_configure = MagicMock()
    mock_structlog_configure = MagicMock()
    mock_instrument_httpx = MagicMock()
    mock_instrument_system_metrics = MagicMock()
    # configure を mock すると実 provider が立たないため、その下流を触る
    # install_exception_redaction も no-op 化する (実呼出は fail-fast する設計)。
    monkeypatch.setattr(
        logfire_setup_module, "install_exception_redaction", MagicMock()
    )
    monkeypatch.setattr(
        logfire_setup_module.logfire, "configure", mock_logfire_configure
    )
    monkeypatch.setattr(
        logfire_setup_module.structlog, "configure", mock_structlog_configure
    )
    monkeypatch.setattr(
        logfire_setup_module.logfire, "instrument_httpx", mock_instrument_httpx
    )
    monkeypatch.setattr(
        logfire_setup_module.logfire,
        "instrument_system_metrics",
        mock_instrument_system_metrics,
    )
    return (
        mock_logfire_configure,
        mock_structlog_configure,
        mock_instrument_httpx,
        mock_instrument_system_metrics,
    )


def _processors(mock_structlog_configure: MagicMock) -> list[Any]:
    """``structlog.configure(processors=[...])`` に渡された生 list を取り出す。"""
    return mock_structlog_configure.call_args.kwargs["processors"]


def _has_renderer(processors: list[Any], renderer_cls: type) -> bool:
    """processor 列に ``renderer_cls`` の instance が含まれるか。"""
    return any(isinstance(p, renderer_cls) for p in processors)


# token gate — 外部送信は token がある時しか起きない (Phase 1 安全弁の要)


def test_no_token_passes_none_and_if_token_present(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """token 未設定 → ``token=None`` + ``send_to_logfire="if-token-present"``。"""
    mock_logfire_configure, _, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    kwargs = mock_logfire_configure.call_args.kwargs
    assert kwargs["token"] is None
    assert kwargs["send_to_logfire"] == "if-token-present"
    assert kwargs["console"] is False
    assert kwargs["service_name"] == "vector-api"
    assert kwargs["environment"] == "development"


def test_token_set_passes_secret_value(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """token 設定時は ``SecretStr.get_secret_value()`` の生値が渡る。"""
    mock_logfire_configure, _, _, _ = patched_configures
    monkeypatch.setattr(
        logfire_setup_module.settings,
        "logfire_token",
        SecretStr("pylf_v1_us_xxxxxxxxxxxxxxxx"),
    )
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-worker-analysis")

    kwargs = mock_logfire_configure.call_args.kwargs
    assert kwargs["token"] == "pylf_v1_us_xxxxxxxxxxxxxxxx"
    assert kwargs["send_to_logfire"] == "if-token-present"
    assert kwargs["service_name"] == "vector-worker-analysis"
    assert kwargs["environment"] == "production"


# renderer gate — stdout の見た目は env 別


def test_production_uses_json_renderer_with_format_exc_info(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """prod では JSONRenderer + format_exc_info (singleton) が含まれる。"""
    _, mock_structlog_configure, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-api")

    procs = _processors(mock_structlog_configure)
    assert _has_renderer(procs, structlog.processors.JSONRenderer)
    assert not _has_renderer(procs, structlog.dev.ConsoleRenderer)
    # format_exc_info は singleton instance なので identity 検査が契約。
    assert structlog.processors.format_exc_info in procs


def test_development_uses_console_renderer_without_format_exc_info(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """dev では ConsoleRenderer のみ (format_exc_info は renderer に委ねる)。"""
    _, mock_structlog_configure, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    procs = _processors(mock_structlog_configure)
    assert _has_renderer(procs, structlog.dev.ConsoleRenderer)
    assert not _has_renderer(procs, structlog.processors.JSONRenderer)
    assert structlog.processors.format_exc_info not in procs


# 順序の不変条件 — prod 例外の Logfire ネイティブ表示が壊れないこと


def test_structlog_processor_precedes_format_exc_info_in_production(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``StructlogProcessor`` は ``format_exc_info`` より前に置かれる。

    逆順だと prod で例外が ``format_exc_info`` に消費されて文字列化された後
    Logfire に届くため、native スタックトレース解析に乗らない。Logfire
    (token 設定済) こそ「例外を見たい」環境なので、この順序は構造的契約。
    """
    _, mock_structlog_configure, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-api")

    procs = _processors(mock_structlog_configure)
    lp_idx = next(
        (i for i, p in enumerate(procs) if isinstance(p, LogfireProcessor)),
        None,
    )
    fei_idx = procs.index(structlog.processors.format_exc_info)
    assert lp_idx is not None, "LogfireProcessor (StructlogProcessor) missing"
    assert lp_idx < fei_idx, (
        "LogfireProcessor must precede format_exc_info so Logfire sees raw exc_info"
    )


def test_structlog_processor_always_present(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``StructlogProcessor`` は env を問わず常にチェーンに乗る。

    Logfire への集約は token の有無で no-op 化されるため、processor を常時
    挿しても dev で外部送信は発生しない (二重防御)。
    """
    _, mock_structlog_configure, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)

    for env_value in ("development", "production"):
        mock_structlog_configure.reset_mock()
        monkeypatch.setattr(logfire_setup_module.settings, "env", env_value)
        setup_logfire("vector-api")
        procs = _processors(mock_structlog_configure)
        assert any(isinstance(p, LogfireProcessor) for p in procs), (
            f"LogfireProcessor missing in env={env_value!r}"
        )


def test_structlog_processor_is_logfire_reexport() -> None:
    """``logfire.StructlogProcessor`` が ``LogfireProcessor`` の re-export である。

    本テストは契約の出所を pin する: logfire 側で名前/実体がずれたら
    型検査ベースの assert (上記 4 件) が全滅する前にここで気付ける。
    """
    assert logfire.StructlogProcessor is LogfireProcessor


# httpx auto-instrument — PII off の構造的契約 (Phase 2)


def test_setup_logfire_calls_instrument_httpx_once(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``setup_logfire`` は ``logfire.instrument_httpx`` を **1 度** 呼ぶ。

    複数回呼ばれると `AsyncClient.send` への monkey-patch が積み重なる懸念が
    あり、また「プロセスごとに 1 度」契約 (Phase 1 と整合) を pin する。
    """
    _, _, mock_instrument_httpx, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    assert mock_instrument_httpx.call_count == 1


def test_setup_logfire_passes_pii_off_kwargs_to_instrument_httpx(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``instrument_httpx`` に PII off の 3 kwargs が **明示的に** 渡される。

    source default と一致するが、将来 default が変わっても安全側に倒れる
    構造的契約 (feedback_structural_guarantee)。``capture_request_body=True``
    に逆転すると AI provider への prompt / 翻訳結果が span に乗る経路ができる
    ため、明示で塞ぐ意義が最も大きい kwarg。
    """
    _, _, mock_instrument_httpx, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-api")

    kwargs = mock_instrument_httpx.call_args.kwargs
    assert kwargs["capture_headers"] is False
    assert kwargs["capture_request_body"] is False
    assert kwargs["capture_response_body"] is False


# 例外 redaction — export 前に生 str(exc) を落とす (PII 封じ込め)


def test_setup_logfire_installs_exception_redaction(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock, MagicMock],
) -> None:
    """``setup_logfire`` は configure 後に ``install_exception_redaction`` を呼ぶ。

    例外貫通で span に乗る生 str(exc) を export 前に redact する経路を pin する。
    無効化されると任意 PII が Logfire span に残る。
    """
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")
    mock_install = MagicMock()
    monkeypatch.setattr(
        logfire_setup_module, "install_exception_redaction", mock_install
    )

    setup_logfire("vector-api")

    assert mock_install.call_count == 1


def test_install_exception_redaction_runs_after_configure(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock, MagicMock],
) -> None:
    """``install_exception_redaction`` は ``logfire.configure`` の **後** に呼ばれる。

    redactor は provider が立った後でないと設置できないため、順序が構造的契約。
    """
    mock_configure, _, _, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")
    order: list[str] = []
    mock_configure.side_effect = lambda *a, **k: order.append("configure")
    monkeypatch.setattr(
        logfire_setup_module,
        "install_exception_redaction",
        MagicMock(side_effect=lambda: order.append("install")),
    )

    setup_logfire("vector-api")

    assert order == ["configure", "install"]


# system メトリクス — OOM 予兆監視の収集対象 (Phase 1) / token gate


def test_instrument_system_metrics_called_with_memory_config_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock, MagicMock],
) -> None:
    """token 設定時に ``instrument_system_metrics`` が VM available + プロセス RSS の
    2 メトリクスだけを ``base=None`` で **1 度** 観測する。

    収集対象を pin する: 余計な basic セット (cpu/swap) を出す退行や、メトリクス名の
    取り違えをここで落とす。VM available = 逼迫判定 / process RSS = 犯人特定。
    """
    _, _, _, mock_instrument_system_metrics = patched_configures
    monkeypatch.setattr(
        logfire_setup_module.settings,
        "logfire_token",
        SecretStr("pylf_v1_us_xxxxxxxxxxxxxxxx"),
    )
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-worker-collection")

    assert mock_instrument_system_metrics.call_count == 1
    args, kwargs = mock_instrument_system_metrics.call_args
    assert args[0] == {
        "system.memory.utilization": ["available"],
        "process.memory.usage": None,
    }
    assert kwargs["base"] is None


def test_instrument_system_metrics_not_called_without_token(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock, MagicMock],
) -> None:
    """token 未設定 (dev/CI/test) では ``instrument_system_metrics`` を呼ばない。

    受け手の無い env で 60s 周期の psutil コールバック収集器を立てない gate 契約。
    logfire 自体が ``send_to_logfire="if-token-present"`` で no-op になるのと整合。
    """
    _, _, _, mock_instrument_system_metrics = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    assert mock_instrument_system_metrics.call_count == 0


# 実チェーンの妥当性 — bootstrap 後の logger 呼出が例外を投げない


def test_logger_works_after_bootstrap_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap → ``logger.info(...)`` が dev で例外を投げない (チェーン妥当)。

    本テストは ``structlog.configure`` を patch せず実状態に書き込むため、
    最後に既定設定へ戻して state 汚染を避ける。``logfire.configure`` および
    ``logfire.instrument_httpx`` は patch で外部送信 / module-level patch を
    遮断する (token 無しでも念のため二重防御)。
    """
    # 外部送信ゼロを保証するため logfire.configure は no-op に。
    monkeypatch.setattr(logfire_setup_module.logfire, "configure", MagicMock())
    # configure が no-op のため実 provider が無い。redactor 設置も no-op に。
    monkeypatch.setattr(
        logfire_setup_module, "install_exception_redaction", MagicMock()
    )
    # AsyncClient.send への global patch も他テスト並走で副作用にならないよう no-op に。
    monkeypatch.setattr(logfire_setup_module.logfire, "instrument_httpx", MagicMock())
    # token=None なので gate により未呼出だが、実収集器の起動を防御的に no-op 化する。
    monkeypatch.setattr(
        logfire_setup_module.logfire, "instrument_system_metrics", MagicMock()
    )
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    try:
        setup_logfire("vector-api")
        log = structlog.get_logger("vector.test")
        # 例外なく完了することのみ確認 (出力内容は本テストの対象外)。
        log.info("logfire_setup_smoke", attr="value")
        try:
            raise RuntimeError("smoke")
        except RuntimeError:
            log.exception("logfire_setup_exception_smoke")
    finally:
        structlog.reset_defaults()
