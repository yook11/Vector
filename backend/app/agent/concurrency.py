"""Researcher と AnsweringRunner が共有する汎用 async 配管。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

__all__ = ["gather_cancel_on_error"]


async def gather_cancel_on_error[ResultT](
    *awaitables: Awaitable[ResultT],
) -> list[ResultT]:
    """未分類例外時に兄弟処理をcancelして合流してから元の例外を返す。"""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
