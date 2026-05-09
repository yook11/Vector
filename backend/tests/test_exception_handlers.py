"""``app.exception_handlers`` の allowlist テスト (red-team chain θ-1)。

検証する性質:
- 許可 form (`<Entity> not found` / `<Entity> already exists`) はそのまま返る
- 内部 ID / source 名 / 改行入り / 空文字などの diverging detail は generic
  message に丸められ、warn ログ (length + sha256 prefix のみ) を残す
- log injection 防止のため raw 文字列はログに焼かれない
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from app.exception_handlers import duplicate_handler, not_found_handler
from app.exceptions import DuplicateError, NotFoundError

# ---------------------------------------------------------------------------
# A. 許可 form は passthrough (must-pass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    [
        "News article not found",
        "News source not found",
        "Watchlist item not found",
    ],
)
async def test_not_found_passthrough_for_allowed_forms(detail: str) -> None:
    response = await not_found_handler(MagicMock(), NotFoundError(detail))
    assert response.status_code == 404
    assert detail.encode() in response.body


@pytest.mark.asyncio
async def test_duplicate_passthrough_for_allowed_form() -> None:
    response = await duplicate_handler(
        MagicMock(), DuplicateError("Watchlist entry already exists")
    )
    assert response.status_code == 409
    assert b"Watchlist entry already exists" in response.body


# ---------------------------------------------------------------------------
# B. allowlist 違反は generic message に丸める (must-block)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaky_detail",
    [
        "Article 12345 not found",
        "Source 'attacker.com' not found",
        "User user@example.com not found",
        "article_id=123 not found in news_articles table",
        "news article not found",  # 小文字始まり
        "  News article not found",  # 前置 whitespace
        "Article\nID not found",  # 改行
        "x" * 200 + " not found",  # 超長
        "",
    ],
)
async def test_not_found_blocks_disallowed_details(leaky_detail: str) -> None:
    with capture_logs() as logs:
        response = await not_found_handler(MagicMock(), NotFoundError(leaky_detail))

    assert response.status_code == 404
    assert b'"detail":"Resource not found"' in response.body
    # raw 文字列は body に出ていない
    if leaky_detail.strip():
        assert leaky_detail.encode() not in response.body

    warn_logs = [
        log for log in logs if log["event"] == "exception_detail_blocked_by_allowlist"
    ]
    assert len(warn_logs) == 1
    # log injection 防止: raw 文字列が log に焼かれていない
    log = warn_logs[0]
    assert "raw_detail" not in log
    assert "raw_detail_length" in log
    assert "raw_detail_sha256_prefix" in log
    assert log["raw_detail_length"] == len(leaky_detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaky_detail",
    [
        "Article 12 already exists",
        "Source 'attacker.com' already exists",
        "Watchlist entry already in DB",  # 別 form
        "watchlist entry already exists",  # 小文字始まり
    ],
)
async def test_duplicate_blocks_disallowed_details(leaky_detail: str) -> None:
    with capture_logs() as logs:
        response = await duplicate_handler(MagicMock(), DuplicateError(leaky_detail))

    assert response.status_code == 409
    assert b'"detail":"Resource already exists"' in response.body
    assert leaky_detail.encode() not in response.body
    warn_logs = [
        log for log in logs if log["event"] == "exception_detail_blocked_by_allowlist"
    ]
    assert len(warn_logs) == 1


# ---------------------------------------------------------------------------
# C. 許可 form では warn ログを焼かない (二重 redact / log noise なし)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_does_not_emit_warning() -> None:
    with capture_logs() as logs:
        await not_found_handler(MagicMock(), NotFoundError("News article not found"))

    warn_logs = [
        log for log in logs if log["event"] == "exception_detail_blocked_by_allowlist"
    ]
    assert warn_logs == []
