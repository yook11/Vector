"""例外チェーンを FQN リスト化する pure helper。

監査 payload の ``error_chain`` field を組み立てる SSoT。session も I/O も持たない
純粋関数で、各 stage の ``audit.stages.*`` から再利用される。
"""

from __future__ import annotations

_MAX_CHAIN_DEPTH = 8


def extract_error_chain(exc: BaseException) -> list[str]:
    """``__cause__`` / ``__context__`` を辿って FQN リスト化。

    深さ上限 ``_MAX_CHAIN_DEPTH`` + ``id()`` 集合で循環防止。``__cause__``
    優先、無ければ ``__context__``。
    """
    chain: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(chain) < _MAX_CHAIN_DEPTH:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        chain.append(f"{type(cur).__module__}.{type(cur).__qualname__}")
        cur = cur.__cause__ or cur.__context__
    return chain
