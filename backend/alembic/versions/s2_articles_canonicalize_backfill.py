"""``articles.source_url`` を canonicalize 済み値に格上げする (PR-E)。

PR-D で ``pending_html_articles.url`` を canonicalize 済み値で持たせたのに対し、
``articles.source_url`` は依然として非正規化値で UNIQUE が効いている状態だった。
PR-E では ``articles.source_url`` を canonicalize 済み値に backfill して、
PR-F で ``article_urls`` テーブルが消えても dedup 強度を維持できるようにする。

upgrade 手順:

1. 全 ``articles`` 行を SELECT
2. Python 側で canonicalize → ``canonical → [ids]`` dict 集計
3. 重複 (canonicalize 後同一 URL に解決される複数行) があれば ``RuntimeError``
   で **migration を fail**。CASCADE DELETE は絶対にしない (``articles`` 削除は
   ``article_extractions`` / ``article_analyses`` / ``article_embeddings`` /
   ``watchlist_entries`` / ``pipeline_events`` に CASCADE 連鎖する)
4. 値が変わる行のみ batch ``UPDATE articles SET source_url = ...``
5. UNIQUE / CHECK 制約は触らない (canonicalize 済み値でそのまま効く)
6. ``pending_html_articles.article_url_id`` を nullable に変更 (PR-E ingestion
   からは ``url`` のみで投入され ``article_url_id`` は NULL になるため)。
   articles.article_url_id は既に nullable (r2 で変更済)。
7. column 自体の物理削除はしない (PR-F に持ち越し)

migration-local ``_canonicalize_url`` を埋め込む理由:
``app/collection/url_canonicalize.py`` を import すると、将来 runtime コードが
リネーム / 削除されたとき過去 migration が壊れる。alembic は時間軸を遡って
実行可能であるべきなので、migration ごとに helper をフリーズコピーする
(PR-D ``s1_pending_url_column`` と同じ方針)。

Revision ID: s2_articles_canonicalize
Revises: s1_pending_url_column
Create Date: 2026-05-06
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import sqlalchemy as sa

from alembic import op

revision: str = "s2_articles_canonicalize"
down_revision: str | None = "s1_pending_url_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# migration-local frozen helper
# ``app/collection/url_canonicalize.py`` (commit 0372d7f) の挙動を凍結コピー。
# runtime のリネーム / 削除に追従しない (= 過去 migration の自己完結性を優先)。
# PR-D ``s1_pending_url_column.py`` の helper と完全に同一。
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
    """frozen from ``app/collection/url_canonicalize.py`` at commit 0372d7f.

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
    connection = op.get_bind()

    rows = connection.execute(
        sa.text("SELECT id, source_url FROM articles")
    ).fetchall()

    # 重複検出: canonicalize 後に同一 URL に解決される行があれば即 fail
    canonical_to_ids: dict[str, list[int]] = {}
    for row in rows:
        canonical = _canonicalize_url(row.source_url)
        canonical_to_ids.setdefault(canonical, []).append(row.id)

    duplicates = {c: ids for c, ids in canonical_to_ids.items() if len(ids) > 1}
    if duplicates:
        sample = list(duplicates.items())[:5]
        raise RuntimeError(
            f"canonicalize 後に source_url が衝突する articles 行が "
            f"{len(duplicates)} 件あります (sample: {sample})。"
            " articles 削除は article_extractions / article_analyses / "
            "article_embeddings / watchlist_entries / pipeline_events に "
            "CASCADE 連鎖するため、migration では自動解決しません。"
            " 手動でマージ or 古い行の削除を行ってから再実行してください。"
        )

    # 値が変わる行のみ UPDATE (idempotent な行は触らない)
    for row in rows:
        canonical = _canonicalize_url(row.source_url)
        if canonical != row.source_url:
            connection.execute(
                sa.text("UPDATE articles SET source_url = :u WHERE id = :id"),
                {"u": canonical, "id": row.id},
            )

    # PR-E ingestion で投入される pending 行は article_url_id を持たない
    # (= ``url`` 列が SSoT)。NOT NULL のままだと INSERT が失敗するため
    # nullable 化する。FK / UNIQUE 制約はそのまま (NULL 同士の重複は許容される)。
    op.alter_column(
        "pending_html_articles",
        "article_url_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    # canonicalize は冪等で down-grade 可能だが、元の非正規化値は復元できない
    # ため pass (article_url_id の nullable 化のみ戻す意味は薄い)。
    op.alter_column(
        "pending_html_articles",
        "article_url_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
