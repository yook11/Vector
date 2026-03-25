"""auth スキーマのテーブル参照定義（FK 解決用）。

Better Auth が管理する auth.user テーブルを SQLAlchemy MetaData に登録する。
実テーブルの作成・管理は Better Auth CLI が行うため、ここでは参照のみ。
"""

from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlmodel import SQLModel

auth_user_ref = Table(
    "user",
    SQLModel.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    schema="auth",
)
