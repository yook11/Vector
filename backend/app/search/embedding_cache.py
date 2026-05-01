"""検索クエリ embedding の非同期 Redis キャッシュ。

クエリテキストの SHA256 をキーとして embedding ベクトルをキャッシュし、
繰り返し検索時に embedding provider への呼び出しをスキップする。呼び出し側は
事前に正規化済みのテキストを渡す想定 (``ArticleListParams._normalize_q`` 参照)
で、軽微なバリエーションは同じキーに集約される。Fire-and-forget で安全:
Redis 障害時は ``get`` が None を返し ``set`` は黙って no-op となり、
呼び出し側は新規 embedding 生成にフォールバックする。

ベクトルは provider ごとに別空間。同時に複数 provider のベクトルが
キャッシュ内で共存することはあり得ない (=共存させてはバグ) ため、key には
model 名を含めない。provider 切替時は本キャッシュを flush する運用手順で
不変条件 (現行 provider のベクトルしか入っていない) を担保する。
"""

from __future__ import annotations

import hashlib
import json

import structlog

from app.redis import get_redis

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "embed:query"
_TTL_SECONDS = 7 * 24 * 3600  # 7 日 — 同一入力の embedding は決定的


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


async def get_query_embedding(text: str) -> list[float] | None:
    """*text* に対応するキャッシュ済み embedding を返す。miss/error 時は None。"""
    try:
        client = get_redis()
        raw = await client.get(_cache_key(text))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.warning("embedding_cache_get_failed", exc_info=True)
        return None


async def set_query_embedding(text: str, vector: list[float]) -> None:
    """*text* に対する *vector* を TTL 付きで永続化する。"""
    try:
        client = get_redis()
        await client.set(
            _cache_key(text),
            json.dumps(vector),
            ex=_TTL_SECONDS,
        )
    except Exception:
        logger.warning("embedding_cache_set_failed", exc_info=True)
