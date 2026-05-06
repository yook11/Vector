"""Query parameter schema boundary tests."""

import pytest
from pydantic import ValidationError

from app.schemas.articles import SemanticSearchParams
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


def test_semantic_search_params_normalize_q() -> None:
    params = SemanticSearchParams(q="  AI   Research  ")
    assert params.q == "ai research"


@pytest.mark.parametrize("q", ["   ", "a" * 201])
def test_semantic_search_params_reject_invalid_q(q: str) -> None:
    with pytest.raises(ValidationError):
        SemanticSearchParams(q=q)
