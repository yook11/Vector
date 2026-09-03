"""migration runner の実行要求。"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIGRATION_RUNNER_PROTOCOL_VERSION = 1


def require_revision_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", value) is None or value in {
        "base",
        "head",
        "heads",
    }:
        raise ValueError("a concrete migration revision ID is required")
    return value


class MigrationRunnerRequest(BaseSettings):
    """実行要求をDB接続設定から分離し、通常のAlembicにmodeを要求しない。"""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        frozen=True,
    )

    protocol_version: Literal[1] = Field(validation_alias="MIGRATION_PROTOCOL_VERSION")
    mode: Literal["expand", "contract", "verify"] = Field(
        validation_alias="MIGRATION_MODE"
    )
    expected_start_revision: str | None = Field(
        default=None, validation_alias="MIGRATION_EXPECTED_START_REVISION"
    )
    target_revision: str = Field(validation_alias="MIGRATION_TARGET_REVISION")
    migration_tree_oid: str = Field(validation_alias="MIGRATION_TREE_OID")

    @field_validator("protocol_version", mode="before")
    @classmethod
    def _require_protocol(cls, value: object) -> int:
        if value == str(MIGRATION_RUNNER_PROTOCOL_VERSION) or (
            type(value) is int and value == MIGRATION_RUNNER_PROTOCOL_VERSION
        ):
            return MIGRATION_RUNNER_PROTOCOL_VERSION
        raise ValueError("unsupported migration runner protocol")

    @field_validator("target_revision", "expected_start_revision")
    @classmethod
    def _require_revision(cls, value: str | None) -> str | None:
        return require_revision_id(value) if value is not None else None

    @field_validator("migration_tree_oid")
    @classmethod
    def _require_tree_oid(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("a 40-digit lowercase migration tree OID is required")
        return value

    @model_validator(mode="after")
    def _require_expected_start(self) -> Self:
        if self.mode != "verify" and self.expected_start_revision is None:
            raise ValueError("expand and contract require an expected start revision")
        return self


def load_migration_runner_request() -> MigrationRunnerRequest:
    return MigrationRunnerRequest()
