"""保存済みの記事URLとソースのsite_urlを、WebUrlと記事URLの正規化形へ揃える。

記事URLは重複判定のキーなので、アプリの正規化形と表記が違う行を書き換える。
規則はこの revision 時点のアプリと同じ手順を凍結したもので、結果は導入済みの
pydantic-core (WHATWG URL の実装) に依存する。

重複の扱い:
- 記事の行: 新しい形の行が既にあれば古い行は書き換えずに残す (削除は分析へ連鎖する)。
- 未完成の行: 新しい形の行が既にあれば古い行を削除する (参照する外部キーは無い)。
- 古い行どうしが同じ形になる場合は失敗させる。エラーには行の id だけを載せる。

Revision ID: z32_normalize_web_urls
Revises: z31_grant_investigation
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from urllib.parse import parse_qsl, urlencode

from pydantic import AnyHttpUrl
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "z32_normalize_web_urls"
down_revision: str | None = "z31_grant_investigation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 既存行のUPDATE・DELETEは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"

logger = logging.getLogger("alembic.runtime.migration")

_MAX_LENGTH = 2048
_TRACKING_PARAMS = frozenset(
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

_SELECT_INCOMPLETE = "SELECT id, url FROM incomplete_articles"
_UPDATE_INCOMPLETE = (
    "UPDATE incomplete_articles SET url = :new WHERE id = :id AND url = :old"
)
_DELETE_INCOMPLETE = "DELETE FROM incomplete_articles WHERE id IN :ids"
_SELECT_ANALYZABLE = "SELECT id, source_url FROM analyzable_articles"
_UPDATE_ANALYZABLE = (
    "UPDATE analyzable_articles SET source_url = :new "
    "WHERE id = :id AND source_url = :old"
)
_SELECT_SOURCES = "SELECT id, site_url, endpoint_url FROM news_sources"
_UPDATE_SITE_URL = (
    "UPDATE news_sources SET site_url = :new WHERE id = :id AND site_url = :old"
)


def _web_url(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > _MAX_LENGTH:
        raise ValueError("not a valid web URL")
    normalized = str(AnyHttpUrl(value))
    if len(normalized) > _MAX_LENGTH:
        raise ValueError("not a valid web URL")
    return normalized


def _canonicalize(url: str) -> str:
    parsed = AnyHttpUrl(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    pairs = parse_qsl(parsed.query or "", keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k.lower() not in _TRACKING_PARAMS]
    return str(
        AnyHttpUrl.build(
            scheme=parsed.scheme,
            username=parsed.username,
            password=parsed.password,
            host=parsed.host or "",
            port=parsed.port,
            path=path[1:],
            query=urlencode(filtered, doseq=True) or None,
        )
    )


def _article_url(raw: str) -> str:
    return _web_url(_canonicalize(_web_url(raw)))


def _changes(
    label: str, rows: Sequence[tuple[int, str]], rule: Callable[[str], str]
) -> dict[int, str]:
    """値が変わる行の id と新しい値を返す。"""
    changes: dict[int, str] = {}
    for row_id, value in rows:
        try:
            normalized = rule(value)
        except ValueError:
            raise RuntimeError(f"{label}: cannot normalize id={row_id}") from None
        if normalized != value:
            changes[row_id] = normalized
    return changes


def _reject_legacy_collisions(label: str, changes: dict[int, str]) -> None:
    by_value: dict[str, list[int]] = defaultdict(list)
    for row_id, normalized in changes.items():
        by_value[normalized].append(row_id)
    collided = sorted(ids for ids in by_value.values() if len(ids) > 1)
    if collided:
        raise RuntimeError(f"{label}: legacy rows collide ids={collided}")


def _rewrite(bind: Connection, statement: str, row_id: int, old: str, new: str) -> None:
    result = bind.execute(text(statement), {"id": row_id, "old": old, "new": new})
    if result.rowcount != 1:
        raise RuntimeError(f"rewrite did not apply id={row_id}")


def _normalize_article_keys(
    bind: Connection,
    label: str,
    *,
    select: str,
    update: str,
    delete: str | None,
) -> None:
    """新しい形の行が既にある古い行は、delete があれば削除し、無ければ残す。"""
    rows = bind.execute(text(select)).tuples().all()
    values = dict(rows)
    existing = set(values.values())
    changes = _changes(label, rows, _article_url)
    _reject_legacy_collisions(label, changes)

    superseded = sorted(i for i, value in changes.items() if value in existing)
    for row_id, normalized in changes.items():
        if normalized not in existing:
            _rewrite(bind, update, row_id, values[row_id], normalized)
    if superseded and delete is not None:
        result = bind.execute(
            text(delete).bindparams(bindparam("ids", expanding=True)),
            {"ids": superseded},
        )
        if result.rowcount != len(superseded):
            raise RuntimeError(f"{label}: delete did not apply ids={superseded}")
    logger.info(
        "%s: rewritten=%d superseded=%d",
        label,
        len(changes) - len(superseded),
        len(superseded),
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # 判定から書き換えまでの間に新しい行が入らないよう書き込みだけを止め、
    # 補完の確定 (未完成の行の削除 → 記事の行の追加) と同じ順で取る。
    op.execute(
        "LOCK TABLE incomplete_articles, analyzable_articles "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    _normalize_article_keys(
        bind,
        "incomplete_articles.url",
        select=_SELECT_INCOMPLETE,
        update=_UPDATE_INCOMPLETE,
        delete=_DELETE_INCOMPLETE,
    )
    _normalize_article_keys(
        bind,
        "analyzable_articles.source_url",
        select=_SELECT_ANALYZABLE,
        update=_UPDATE_ANALYZABLE,
        delete=None,
    )

    sources = bind.execute(text(_SELECT_SOURCES)).tuples().all()
    # endpoint_url は一意のキーで、既存行は正規化済みの前提なので変わるなら止める。
    endpoint_changes = _changes(
        "news_sources.endpoint_url", [(i, e) for i, _, e in sources], _web_url
    )
    if endpoint_changes:
        raise RuntimeError(
            f"news_sources.endpoint_url: would change ids={sorted(endpoint_changes)}"
        )
    site_values = {i: s for i, s, _ in sources}
    site_changes = _changes(
        "news_sources.site_url", list(site_values.items()), _web_url
    )
    for row_id, normalized in site_changes.items():
        _rewrite(bind, _UPDATE_SITE_URL, row_id, site_values[row_id], normalized)
    logger.info("news_sources.site_url: rewritten=%d", len(site_changes))


def downgrade() -> None:
    # 正規化前の表記と削除した重複は復元できず、後続の往復試験もここを越えて戻る。
    pass
