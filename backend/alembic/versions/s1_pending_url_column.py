"""``pending_html_articles.url`` 列を追加して dual-write/dual-read を始める (PR-D)。

``article_urls`` テーブル廃止プラン (PR-D / PR-E / PR-F) の最初の PR。
PR-D 時点では ``article_url_id`` 経路を残したまま、新 ``url`` 列を canonicalize
済み値で埋めて dual-write/dual-read 期間に入る。物理削除は PR-F。

upgrade 手順 (既存行への NOT NULL 追加で fail しないよう staged 投入):

1. ``ADD COLUMN url VARCHAR(2048) NULL``
2. backfill: 既存 pending 行を ``_canonicalize_url(article_urls.normalized_url)``
   で埋める (Python batch UPDATE)
3. NULL 残存チェック (RAISE)
4. ``ALTER COLUMN url SET NOT NULL``
5. ``ADD UNIQUE(url)`` (``uq_pending_html_articles_url``)
6. ``ADD CHECK(url ~ '^https?://.+')`` (``ck_pending_html_articles_url_scheme``)

PR-D で canonicalize 込み backfill する理由:
PR2.5-A (``r1_pending_html_articles``) の backfill は ``articles.source_url``
を ``article_urls.normalized_url`` に生コピーしているため legacy 行は
canonicalize されていない (列名に騙されない)。pending.url を canonicalize 済み
値で持っておくと、PR-D 〜 PR-E 間に走る pending → articles 遷移で URL 不整合が
起きない。

migration-local ``_canonicalize_url`` を埋め込む理由:
``app/collection/url_canonicalize.py`` を import すると、将来 runtime コードが
リネーム / 削除されたとき過去 migration が壊れる。alembic は時間軸を遡って
実行可能であるべきなので、migration ごとに helper をフリーズコピーする。

Revision ID: s1_pending_url_column
Revises: r3_drop_discovered_articles
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import sqlalchemy as sa

from alembic import op

revision: str = "s1_pending_url_column"
down_revision: str | None = "r3_drop_discovered_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# migration-local frozen helper
# ``app/collection/url_canonicalize.py`` (commit ac02631) の挙動を凍結コピー。
# runtime のリネーム / 削除に追従しない (= 過去 migration の自己完結性を優先)。
# ---------------------------------------------------------------------------
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "referrer",
    }
)


def _canonicalize_url(raw: str) -> str:
    """frozen from ``app/collection/url_canonicalize.py`` at commit ac02631.

    1. lowercase host
    2. tracking parameters strip (UTM / 広告クリック ID / Mailchimp / ref)
    3. trailing slash 正規化 (root ``/`` は保持)
    4. fragment 除去
    5. scheme 保存
    """
    parsed = urlparse(raw)
    netloc = parsed.netloc.lower()

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k.lower() not in _TRACKING_PARAMS]
    new_query = urlencode(filtered, doseq=True)

    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunparse((parsed.scheme, netloc, path, parsed.params, new_query, ""))


def upgrade() -> None:
    # 1. ADD COLUMN url (NULL 許容で投入)
    op.add_column(
        "pending_html_articles",
        sa.Column("url", sa.String(length=2048), nullable=True),
    )

    # 2. backfill: 既存 pending 行を canonicalize 済み URL で埋める
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT p.id, au.normalized_url "
            "FROM pending_html_articles p "
            "JOIN article_urls au ON au.id = p.article_url_id"
        )
    ).fetchall()
    for row in rows:
        canonical = _canonicalize_url(row.normalized_url)
        connection.execute(
            sa.text("UPDATE pending_html_articles SET url = :u WHERE id = :id"),
            {"u": canonical, "id": row.id},
        )

    # 3. NULL 残存チェック: backfill 漏れ (FK 切れ等) を運用前に検出
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE unfilled int; "
            "BEGIN "
            "  SELECT COUNT(*) INTO unfilled FROM pending_html_articles "
            "  WHERE url IS NULL; "
            "  IF unfilled > 0 THEN "
            "    RAISE EXCEPTION '% pending_html_articles rows still have NULL "
            "url after backfill', unfilled; "
            "  END IF; "
            "END $$;"
        )
    )

    # 4. SET NOT NULL
    op.alter_column("pending_html_articles", "url", nullable=False)

    # 5. UNIQUE
    op.create_unique_constraint(
        "uq_pending_html_articles_url",
        "pending_html_articles",
        ["url"],
    )

    # 6. CHECK (scheme 検証)
    op.create_check_constraint(
        "ck_pending_html_articles_url_scheme",
        "pending_html_articles",
        "url ~ '^https?://.+'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pending_html_articles_url_scheme",
        "pending_html_articles",
        type_="check",
    )
    op.drop_constraint(
        "uq_pending_html_articles_url",
        "pending_html_articles",
        type_="unique",
    )
    op.drop_column("pending_html_articles", "url")
