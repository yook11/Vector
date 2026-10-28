"""``app.exception_handlers`` が対象の種類から404の応答を作る試験(DBなし)。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.exception_handlers import not_found_handler
from app.exceptions import NotFoundError, NotFoundResource


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "expected_detail"),
    [
        (NotFoundResource.NEWS_ARTICLE, "News article not found"),
        (NotFoundResource.CATEGORY, "Category not found"),
        (NotFoundResource.NEWS_SOURCE, "News source not found"),
    ],
)
async def test_not_found_returns_fixed_detail_for_resource(
    resource: NotFoundResource, expected_detail: str
) -> None:
    """対象の種類を渡すと、その種類の決まった文を detail に持つ404を返す。"""
    response = await not_found_handler(MagicMock(), NotFoundError(resource))

    assert (response.status_code, json.loads(bytes(response.body))) == (
        404,
        {"detail": expected_detail},
    )
