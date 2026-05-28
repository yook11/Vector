"""Query parameter schema boundary tests."""

import pytest
from pydantic import ValidationError

from app.schemas.base import PaginationParams


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": 10001},
        {"perPage": 0},
        {"perPage": 101},
    ],
)
def test_pagination_params_reject_invalid_bounds(params: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        PaginationParams(**params)


def test_pagination_params_default_per_page_is_24() -> None:
    # frontend の PerPageSelect allowlist ["12","24","48","100"] と
    # 一致させ続けるための SSoT 固定。値を変える場合は per-page.ts も更新。
    assert PaginationParams().per_page == 24
    assert PaginationParams().page == 1
