"""期限を過ぎた認証カウンターを追加の列参照なしで削除する。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def delete_expired_counters(connection: AsyncConnection, cutoff_ms: int) -> int:
    """transactionの確定を呼び出し側に委ね、削除件数を返す。"""
    result = await connection.execute(
        text('DELETE FROM auth."rateLimit" WHERE "lastRequest" < :cutoff_ms'),
        {"cutoff_ms": cutoff_ms},
    )
    return result.rowcount
