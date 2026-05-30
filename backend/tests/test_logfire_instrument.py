"""``main.py`` の Logfire mapper / sanitize ヘルパーの不変条件テスト。

検証する性質:
- ``_sanitize_validation_errors``: Pydantic v2 の error dict から ``input``
  (送信値) / ``ctx`` (型検査文脈) / ``url`` (docs URL) を落とす。
  残るのは ``type`` / ``loc`` / ``msg`` のみ。
- ``_drop_endpoint_args_on_success``: 成功時は ``None`` (log message 不発)、
  validation error 時は sanitize 済 errors のみ (``values`` / 各 error の
  ``input`` を漏らさない)。
- capfire 経由で実 FastAPI app に instrument_fastapi を当て、捕捉した span / log
  に rejected input が含まれないことを JSON 全文検索で検証する。
- capfire fixture は内部で ``logfire.configure(send_to_logfire=False, ...)`` を
  呼ぶため、capfire を使うテストでは ``setup_logfire`` を呼ばない (二重
  configure を避け capfire の TestExporter を活かす)。
"""

from __future__ import annotations

import json
from typing import Any

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from logfire.testing import CaptureLogfire
from pydantic import BaseModel, Field

from app.main import _drop_endpoint_args_on_success, _sanitize_validation_errors

# ---------------------------------------------------------------------------
# _sanitize_validation_errors — rejected input 除去の単体テスト
# ---------------------------------------------------------------------------


def test_sanitize_drops_input_ctx_url_keeps_type_loc_msg() -> None:
    """``input`` / ``ctx`` / ``url`` を落とし ``type`` / ``loc`` / ``msg`` を残す。

    Pydantic v2 ``errors()`` の標準鍵を網羅的に列挙し、残し / 落とし の境界を
    具体値で確認する。``input`` は sensitive value を入れて全文検索する。
    """
    sensitive = "sensitive_q_value_abcdef123456"
    raw = [
        {
            "type": "string_too_long",
            "loc": ("body", "q"),
            "msg": "String should have at most 10 characters",
            "input": sensitive,
            "ctx": {"max_length": 10},
            "url": "https://errors.pydantic.dev/2.5/v/string_too_long",
        }
    ]

    out = _sanitize_validation_errors(raw)

    assert len(out) == 1
    assert set(out[0].keys()) == {"type", "loc", "msg"}
    assert out[0]["type"] == "string_too_long"
    assert out[0]["loc"] == ("body", "q")
    assert out[0]["msg"] == "String should have at most 10 characters"
    # sensitive 文字列が **どこにも** 残っていない (再帰的全文検索)。
    assert sensitive not in json.dumps(out, default=str)


def test_sanitize_handles_multiple_errors() -> None:
    """複数 error を独立に処理 (片方の ``input`` 漏れが他方に紛れない)。"""
    s1 = "leaked_value_one_xxxxxxxx"
    s2 = "leaked_value_two_yyyyyyyy"
    raw = [
        {"type": "missing", "loc": ("body", "a"), "msg": "Field required", "input": s1},
        {
            "type": "value_error",
            "loc": ("body", "b"),
            "msg": "Value error",
            "input": s2,
        },
    ]

    out = _sanitize_validation_errors(raw)

    assert len(out) == 2
    dumped = json.dumps(out, default=str)
    assert s1 not in dumped
    assert s2 not in dumped


def test_sanitize_empty_returns_empty() -> None:
    """error 0 件なら返却も ``[]`` (mapper 側の `if errors:` 判定と整合)。"""
    assert _sanitize_validation_errors([]) == []


def test_sanitize_tolerates_partial_dict() -> None:
    """鍵欠落でも例外を投げず ``None`` で埋める (防御的実装の保証)。

    logfire / FastAPI / Pydantic 側で error 形が将来変わっても、欠けた鍵は
    None として残り mapper 全体が落ちない (= mapper 経由の span 抜けで PII
    が裸で出る事故を防ぐ)。
    """
    out = _sanitize_validation_errors([{"type": "missing"}])
    assert out == [{"type": "missing", "loc": None, "msg": None}]


# ---------------------------------------------------------------------------
# _drop_endpoint_args_on_success — 成功 / 失敗の非対称契約
# ---------------------------------------------------------------------------


def _fake_request() -> Any:
    """mapper の第 1 引数は使わないので最小限の sentinel を渡す。"""
    return object()


def test_mapper_returns_none_on_success() -> None:
    """成功 (``errors`` が空 list) なら ``None`` (log message 不発)。"""
    attrs = {"values": {"q": "stripe"}, "errors": []}
    assert _drop_endpoint_args_on_success(_fake_request(), attrs) is None


def test_mapper_returns_none_when_errors_key_missing() -> None:
    """``errors`` 鍵そのものが無い場合も ``None`` (防御的)。"""
    attrs = {"values": {"q": "stripe"}}
    assert _drop_endpoint_args_on_success(_fake_request(), attrs) is None


def test_mapper_drops_values_and_input_on_validation_error() -> None:
    """validation error 時は ``errors`` のみ、``values`` と各 ``input`` を除去。

    具体的な sensitive 値で、mapper の返却に PII が残らないことを確認する。
    """
    sensitive_value = "sensitive_long_value_xxxxxxxxxxx"
    sensitive_input = "sensitive_rejected_input_zzzzzzzz"
    attrs = {
        "values": {"q": sensitive_value, "limit": 100},
        "errors": [
            {
                "type": "string_too_long",
                "loc": ("body", "q"),
                "msg": "too long",
                "input": sensitive_input,
                "ctx": {"max_length": 10},
                "url": "https://errors.pydantic.dev/2.5/v/string_too_long",
            }
        ],
    }

    out = _drop_endpoint_args_on_success(_fake_request(), attrs)

    assert out is not None
    assert set(out.keys()) == {"errors"}
    assert len(out["errors"]) == 1
    assert set(out["errors"][0].keys()) == {"type", "loc", "msg"}
    dumped = json.dumps(out, default=str)
    assert sensitive_value not in dumped
    assert sensitive_input not in dumped


# ---------------------------------------------------------------------------
# capfire oracle — instrument_fastapi 経路で実 span に PII が乗らない
# ---------------------------------------------------------------------------


class _ItemsBody(BaseModel):
    q: str = Field(min_length=1, max_length=10)


def test_validation_error_span_drops_rejected_input(capfire: CaptureLogfire) -> None:
    """instrument_fastapi 経由でも sanitize が経路上効いていることを実検証。

    capfire fixture は ``logfire.configure(send_to_logfire=False, ...)`` を
    自前で呼ぶため、本テスト内では ``setup_logfire`` を呼ばない (二重
    configure 回避 / TestExporter を活かす契約)。
    """
    app = FastAPI()

    # body は 422 で到達しないため pragma: no cover。
    @app.post("/items")
    def post_items(body: _ItemsBody) -> dict:  # pragma: no cover
        return {"ok": True, "q": body.q}

    logfire.instrument_fastapi(
        app,
        request_attributes_mapper=_drop_endpoint_args_on_success,
        capture_headers=False,
        record_send_receive=False,
        extra_spans=False,
    )

    sensitive = "sensitive_long_query_xxxxxxxxxxxxxxxxxxxxxxxxx"
    client = TestClient(app)
    resp = client.post("/items", json={"q": sensitive})
    assert resp.status_code == 422

    # 捕捉した span を JSON 化して全文検索: 送信値が **1 つも** 現れない。
    spans_dump = capfire.exporter.exported_spans_as_dict()
    dumped = json.dumps(spans_dump, default=str)
    assert sensitive not in dumped, (
        f"rejected input leaked into Logfire span: {dumped!r}"
    )


@pytest.fixture(autouse=True)
def _reset_fastapi_instrumentation() -> None:
    yield
