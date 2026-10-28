"""``setup_logfire`` の不変条件テスト。

検証する性質:
- token 未設定 (dev/CI/test) では ``logfire.configure`` に ``token=None`` と
  ``send_to_logfire="if-token-present"`` が渡され、外部送信が **絶対に** 起きない。
- token 設定時は ``SecretStr.get_secret_value()`` の生値が渡る (Settings 経由
  での秘密保管を維持しつつ logfire SDK へは生値が必要)。

注意: ``logfire.configure()`` は global 可変状態なので、本テストは patch して
呼出の検査で完結させ、他テストへの state 汚染を避ける (plan §テスト方針)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.logfire import setup as logfire_setup_module
from app.logfire.setup import setup_logfire


@pytest.fixture
def patched_configures(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """``logfire.configure`` / ``logfire.instrument_httpx``
    / ``logfire.instrument_system_metrics`` を patch して呼出を捕捉。

    Global 可変状態への副作用を完全に遮断するため 3 つすべて MagicMock に差し
    替える。``instrument_httpx`` は ``AsyncClient.send`` の module-level patch
    なので、patch せずに本物を呼ぶと httpx の通常使用が壊れる (テスト並走時)。
    ``instrument_system_metrics`` も patch しないと token 設定時に実 OTel 収集器
    (60s 周期 psutil コールバックスレッド) がテスト内で起動してしまう。
    """
    mock_logfire_configure = MagicMock()
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
        logfire_setup_module.logfire, "instrument_httpx", mock_instrument_httpx
    )
    monkeypatch.setattr(
        logfire_setup_module.logfire,
        "instrument_system_metrics",
        mock_instrument_system_metrics,
    )
    return (
        mock_logfire_configure,
        mock_instrument_httpx,
        mock_instrument_system_metrics,
    )


# token gate — 外部送信は token がある時しか起きない (Phase 1 安全弁の要)


def test_no_token_passes_none_and_if_token_present(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """token 未設定 → ``token=None`` + ``send_to_logfire="if-token-present"``。"""
    mock_logfire_configure, _, _ = patched_configures
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
    mock_logfire_configure, _, _ = patched_configures
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


# httpx auto-instrument — PII off の構造的契約 (Phase 2)


def test_setup_logfire_calls_instrument_httpx_once(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``setup_logfire`` は ``logfire.instrument_httpx`` を **1 度** 呼ぶ。

    複数回呼ばれると `AsyncClient.send` への monkey-patch が積み重なる懸念が
    あり、また「プロセスごとに 1 度」契約 (Phase 1 と整合) を pin する。
    """
    _, mock_instrument_httpx, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    assert mock_instrument_httpx.call_count == 1


def test_setup_logfire_passes_pii_off_kwargs_to_instrument_httpx(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``instrument_httpx`` に PII off の 4 kwargs が **明示的に** 渡される。

    source default と一致するが、将来 default が変わっても安全側に倒れる
    構造的契約 (feedback_structural_guarantee)。``capture_request_body=True``
    に逆転すると AI provider への prompt / 翻訳結果が span に乗る経路ができる
    ため、明示で塞ぐ意義が最も大きい kwarg。
    """
    _, mock_instrument_httpx, _ = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "production")

    setup_logfire("vector-api")

    kwargs = mock_instrument_httpx.call_args.kwargs
    assert kwargs["capture_all"] is False
    assert kwargs["capture_headers"] is False
    assert kwargs["capture_request_body"] is False
    assert kwargs["capture_response_body"] is False


# 例外 redaction — export 前に生 str(exc) を落とす (PII 封じ込め)


def test_setup_logfire_installs_exception_redaction(
    monkeypatch: pytest.MonkeyPatch,
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
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
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """``install_exception_redaction`` は ``logfire.configure`` の **後** に呼ばれる。

    redactor は provider が立った後でないと設置できないため、順序が構造的契約。
    """
    mock_configure, _, _ = patched_configures
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
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """token 設定時に ``instrument_system_metrics`` が VM available + プロセス RSS の
    2 メトリクスだけを ``base=None`` で **1 度** 観測する。

    収集対象を pin する: 余計な basic セット (cpu/swap) を出す退行や、メトリクス名の
    取り違えをここで落とす。VM available = 逼迫判定 / process RSS = 犯人特定。
    """
    _, _, mock_instrument_system_metrics = patched_configures
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
    patched_configures: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """token 未設定 (dev/CI/test) では ``instrument_system_metrics`` を呼ばない。

    受け手の無い env で 60s 周期の psutil コールバック収集器を立てない gate 契約。
    logfire 自体が ``send_to_logfire="if-token-present"`` で no-op になるのと整合。
    """
    _, _, mock_instrument_system_metrics = patched_configures
    monkeypatch.setattr(logfire_setup_module.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup_module.settings, "env", "development")

    setup_logfire("vector-api")

    assert mock_instrument_system_metrics.call_count == 0
