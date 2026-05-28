"""Ready 構築フェーズの失敗分類。"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError


@dataclass(frozen=True, slots=True)
class ReadyBuildFailureProjection:
    """Ready build failed audit 用の最小 projection。"""

    outcome_code: str
    failure_kind: str


def project_ready_build_failure(
    *, stage_prefix: str, exc: Exception
) -> ReadyBuildFailureProjection:
    """Ready 構築フェーズ例外を stage 固有 outcome_code に分類する。"""
    if isinstance(exc, SQLAlchemyError):
        return ReadyBuildFailureProjection(
            outcome_code=f"{stage_prefix}_ready_build_failed_db_error",
            failure_kind="db_error",
        )
    if isinstance(exc, ValidationError):
        return ReadyBuildFailureProjection(
            outcome_code=f"{stage_prefix}_ready_build_failed_contract_invalid",
            failure_kind="contract_invalid",
        )
    return ReadyBuildFailureProjection(
        outcome_code=f"{stage_prefix}_ready_build_failed_unexpected_error",
        failure_kind="unexpected_error",
    )
