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
