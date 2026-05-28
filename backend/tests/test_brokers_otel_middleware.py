"""``brokers.py`` の OpenTelemetryMiddleware 登録と CLIENT_STARTUP hook の不変条件
テスト、および taskiq trace 伝搬 / PII 非含有 / Proxy 遅延束縛の capfire oracle。

検証する性質:

1. **4-1 broker object 状態の不変条件**:
   - 6 broker すべての middleware tuple の **先頭** が ``OpenTelemetryMiddleware``、
     その次が ``SimpleRetryMiddleware`` (= pre_execute FIFO で consumer span が
     retry より外側に open する登録順契約)。
   - ``OpenTelemetryMiddleware`` instance が各 broker に **1 つだけ** (span/metric
     重複出力を防ぐ)。
   - scheduler を持つ 3 broker (metadata / trend_discovery / briefing) に CLIENT_STARTUP
     hook が **1 つ以上** 登録される。残り 3 broker には登録されない。
   - WORKER_STARTUP は全 6 broker に登録される (Phase 1/2 既存契約の回帰検知)。

2. **4-2 taskiq trace 伝搬 capfire oracle**:
   - ``InMemoryBroker`` + ``OpenTelemetryMiddleware`` で kiq → execute フルパスを
     1 process 内で走らせ、producer / consumer span が **同一 trace_id** で親子接続
     することを assert (W3C propagation が message.labels 経由で成立)。

3. **4-3 Proxy 遅延束縛 capfire oracle**:
   - test module top-level (= ``capfire`` fixture の ``logfire.configure(...)`` より
     時系列的に前) で ``OpenTelemetryMiddleware()`` を持つ broker を構築し、
     capfire 経由の exporter に span が届くことを assert。
   - 本テストが落ちる = brokers.py の module-level instantiate を諦め、startup
     フック内 attach への切替が必要 (= Phase 3 設計 (B) の前提が崩れた合図)。

4. **4-4 task args PII 非含有 capfire oracle**:
   - OpenTelemetryMiddleware は task の args / kwargs を span attribute に乗せない
     設計 (Phase 2 4 重防御と整合)。具体的な sensitive 値で全文検索する空虚回避の
     oracle 化。将来 taskiq が ``messaging.task_args`` のような attribute を生やす
     変更を入れたら本テストが回帰検知する。

設計スタンス (feedback_per_seam_mapping_totality_oracle):
- bootstrap 系は global state なので unit テストは「broker object の不変条件」を
  検査するに留め、実 startup の挙動は capfire 経路 (4-2/4-3/4-4) と実機 dashboard
  で確認する。
- capfire fixture は内部で ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶ
  ため、本ファイルでは ``setup_logfire`` を直接呼ばない (二重 configure 回避)。
"""

from __future__ import annotations

import json

import logfire
import pytest
from logfire.testing import CaptureLogfire
from taskiq import InMemoryBroker, SimpleRetryMiddleware, TaskiqEvents
from taskiq.middlewares.opentelemetry_middleware import OpenTelemetryMiddleware

from app.queue.brokers import (
    broker_analysis,
    broker_briefing,
    broker_content,
    broker_embedding,
    broker_metadata,
    broker_trend_discovery,
)

# ---------------------------------------------------------------------------
# 4-1. middleware の identity / 順序 unit テスト (broker object 状態の不変条件)
# ---------------------------------------------------------------------------

_BROKERS_WITH_SCHEDULER = (
    (broker_metadata, "metadata"),
    (broker_trend_discovery, "trend_discovery"),
    (broker_briefing, "briefing"),
)
_BROKERS_WITHOUT_SCHEDULER = (
    (broker_content, "content"),
    (broker_analysis, "analysis"),
    (broker_embedding, "embedding"),
)
_ALL_BROKERS = _BROKERS_WITH_SCHEDULER + _BROKERS_WITHOUT_SCHEDULER


def test_all_brokers_have_otel_middleware_first() -> None:
    """全 broker の middleware 列の先頭が ``OpenTelemetryMiddleware``。

    pre_execute FIFO で consumer span を SimpleRetry より外側に open する登録順
    契約 (1 execute サイクル内の handler 例外は span 範囲に含まれる)。逆順だと
    retry 判定後に span が開き、handler 例外が span 範囲に入らない。
    """
    for broker, label in _ALL_BROKERS:
        assert len(broker.middlewares) >= 2, label
        assert isinstance(broker.middlewares[0], OpenTelemetryMiddleware), label
        assert isinstance(broker.middlewares[1], SimpleRetryMiddleware), label


def test_otel_middleware_singleton_per_broker() -> None:
    """各 broker の OpenTelemetryMiddleware は **1 つだけ**。

    複数挿すと span/metric が重複出力されて Logfire の free tier 消費が倍化する。
    """
    for broker, label in _ALL_BROKERS:
        n = sum(1 for m in broker.middlewares if isinstance(m, OpenTelemetryMiddleware))
        assert n == 1, f"{label}: expected 1 OTel middleware, got {n}"


def test_scheduler_lifecycle_registered_for_cron_brokers_only() -> None:
    """CLIENT_STARTUP hook が cron 駆動 3 broker のみに登録される。

    scheduler を持たない broker (content / analysis / embedding) に登録すると、
    将来「.kiq() の遅延副作用で broker.startup() が走る」変更が入ったときに
    API process で setup_logfire が二重呼出される事故になりうる。
    """
    for broker, label in _BROKERS_WITH_SCHEDULER:
        handlers = broker.event_handlers.get(TaskiqEvents.CLIENT_STARTUP, [])
        assert len(handlers) >= 1, (
            f"{label}: missing CLIENT_STARTUP handler (scheduler bootstrap)"
        )

    for broker, label in _BROKERS_WITHOUT_SCHEDULER:
        handlers = broker.event_handlers.get(TaskiqEvents.CLIENT_STARTUP, [])
        assert len(handlers) == 0, (
            f"{label}: unexpected CLIENT_STARTUP handler (scheduler-less broker)"
        )


def test_worker_lifecycle_registered_for_all_brokers() -> None:
    """WORKER_STARTUP は Phase 1/2 既存契約として全 6 broker に登録される。

    Phase 3 で追加した ``_register_scheduler_lifecycle`` (CLIENT_STARTUP) が
    ``_register_lifecycle`` (WORKER_STARTUP) の登録を上書き / 排他にしていない
    ことの回帰検知。broker_metadata / broker_trend_discovery / broker_briefing は同じ
    broker object に WORKER_STARTUP と CLIENT_STARTUP の 2 種の hook が共存する
    (プロセスが違うため発火は競合せず、ワンプロセス内で両方発火する経路は存在
    しない)。
    """
    for broker, label in _ALL_BROKERS:
        handlers = broker.event_handlers.get(TaskiqEvents.WORKER_STARTUP, [])
        assert len(handlers) >= 1, f"{label}: missing WORKER_STARTUP handler"


# ---------------------------------------------------------------------------
# 4-3. capfire oracle: Proxy provider 遅延束縛 (設計根幹の構造的契約)
# ---------------------------------------------------------------------------
# 重要: ``_PRE_CONFIGURE_BROKER`` は **test module top-level** で構築する。これは
# pytest collection phase で実行され、capfire fixture の ``logfire.configure(...)``
# setup より時系列的に前。OpenTelemetryMiddleware が ``trace.get_tracer(...)`` で
# 取得するのは ProxyTracer になる。capfire fixture が後で configure を呼ぶことで
# ProxyTracerProvider が real provider に再束縛され、module-level の middleware
# が持つ tracer も real tracer に再委譲される (logfire/_internal/tracer.py:53-92)。
# この経路が成立しなければ「configure 前 instantiate」設計が崩れる合図。

_PRE_CONFIGURE_BROKER = InMemoryBroker().with_middlewares(OpenTelemetryMiddleware())


@_PRE_CONFIGURE_BROKER.task
async def _delayed_binding_probe() -> str:
    return "ok"


@pytest.mark.asyncio
async def test_otel_middleware_binds_lazily_to_logfire_provider(
    capfire: CaptureLogfire,
) -> None:
    """設計根幹 (B): configure 前に作った middleware が configure 後の exporter に
    span を流す構造的契約。

    module top-level の ``OpenTelemetryMiddleware()`` 生成は pytest collection
    phase で走る → capfire fixture の ``logfire.configure(...)`` setup より時系列
    的に前 → ProxyTracerProvider が install 後に既存 tracer を real tracer に再
    束縛する設計が効いていなければ、span は exporter に到達しない。

    本テストが落ちる = Phase 3 設計の前提が崩れた合図 (brokers.py の module-level
    instantiate を諦め、startup フック内 attach への切替が必要)。
    """
    await _PRE_CONFIGURE_BROKER.startup()
    try:
        task = await _delayed_binding_probe.kiq()
        result = await task.wait_result()
        assert result.return_value == "ok"
    finally:
        await _PRE_CONFIGURE_BROKER.shutdown()

    spans = capfire.exporter.exported_spans_as_dict()
    producer_spans = [s for s in spans if s["name"].startswith("send/")]
    consumer_spans = [s for s in spans if s["name"].startswith("execute/")]
    assert len(producer_spans) >= 1, (
        "Proxy 遅延束縛が成立していない: pre_send span が exporter に届かない"
    )
    assert len(consumer_spans) >= 1, (
        "Proxy 遅延束縛が成立していない: pre_execute span が exporter に届かない"
    )


# ---------------------------------------------------------------------------
# 4-2. capfire oracle: kiq → execute フルパスで traceparent が message.labels 経由
# で伝搬する (producer span / consumer span が同一 trace_id で親子接続)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kiq_propagates_traceparent_via_labels(
    capfire: CaptureLogfire,
) -> None:
    """``InMemoryBroker.kiq() → wait_result()`` で producer / consumer span が同一
    trace_id を共有することを oracle 化する。

    本テストは「OpenTelemetryMiddleware が message.labels 経由で traceparent を
    inject → extract する経路全体が成立する」ことを oracle 化する。middleware の
    object 構造ではなく、kicker → 受信 receiver までの 1 trace 成立を検査する点が
    4-1 との分業 (4-1 は登録、4-2 は経路)。
    """
    broker = InMemoryBroker().with_middlewares(OpenTelemetryMiddleware())

    @broker.task
    async def _noop(value: str) -> str:
        return value

    await broker.startup()
    try:
        with logfire.span("parent_pipeline_stage"):
            task = await _noop.kiq("hello")
            result = await task.wait_result()
        assert result.return_value == "hello"
    finally:
        await broker.shutdown()

    spans = capfire.exporter.exported_spans_as_dict()
    parent_spans = [s for s in spans if s["name"] == "parent_pipeline_stage"]
    producer_spans = [s for s in spans if s["name"].startswith("send/")]
    consumer_spans = [s for s in spans if s["name"].startswith("execute/")]

    assert len(parent_spans) == 1, "parent span not exported"
    assert len(producer_spans) >= 1, "producer span (pre_send) not exported"
    assert len(consumer_spans) >= 1, "consumer span (pre_execute) not exported"

    parent = parent_spans[0]
    producer = producer_spans[0]
    consumer = consumer_spans[0]

    # producer は parent_pipeline_stage の子 (= API span を親に持つ実本番経路と同形)。
    assert producer["parent"]["span_id"] == parent["context"]["span_id"], (
        "producer span is not a child of the logfire.span parent"
    )
    # consumer は producer と **同一 trace_id**、かつ parent が producer span
    # (W3C propagation が message.labels 経由で成立している証拠)。
    assert consumer["context"]["trace_id"] == producer["context"]["trace_id"], (
        "consumer trace_id mismatch: traceparent did not propagate via labels"
    )
    assert consumer["parent"]["span_id"] == producer["context"]["span_id"], (
        "consumer parent is not the producer span"
    )


# ---------------------------------------------------------------------------
# 4-4. capfire oracle: task args が span attribute に漏れない (PII 非含有契約)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otel_middleware_does_not_leak_task_args_into_spans(
    capfire: CaptureLogfire,
) -> None:
    """OpenTelemetryMiddleware は task の args / kwargs を span attribute に乗せ
    ない (Phase 2 4 重防御と整合)。

    具体的な sensitive 値で oracle 化 (空虚回避): 将来 taskiq が
    ``messaging.task_args`` のような attribute を生やす変更を入れたら、本テストが
    回帰検知する。記事本文 / URL / prompt 等が将来 task 引数として渡されたケースで
    Logfire dashboard に焼かれない構造的契約。
    """
    broker = InMemoryBroker().with_middlewares(OpenTelemetryMiddleware())

    @broker.task
    async def _echo(payload: str) -> str:
        return payload

    sensitive = "sensitive_article_body_xxxxxxxxxxxxxxxxxxxx"

    await broker.startup()
    try:
        task = await _echo.kiq(sensitive)
        result = await task.wait_result()
        assert result.return_value == sensitive
    finally:
        await broker.shutdown()

    dump = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)
    assert sensitive not in dump, (
        f"task argument leaked into OTel span attributes: {dump!r}"
    )
