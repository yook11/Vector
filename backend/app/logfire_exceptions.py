"""Vector domain exception の基底クラス。Logfire SaaS への PII 流出を構造的に防ぐ。

経路: Phase 1 で structlog → Logfire 経路を確立、Phase 2/3 で FastAPI /
SQLAlchemy / httpx / taskiq の span に ``exception.message`` attribute が
焼かれるようになった。exception の ``__str__`` に PII (URL / 本文 / prompt /
AI response) が含まれると Logfire SaaS に流出する。

本 module は **base class で ``__str__`` を class name + SAFE_ATTRS の固定
形式に縛る** ことで、subclass が constructor に何を渡しても ``__str__``
経路では PII が SaaS に乗らない構造的契約を作る
(``feedback_structural_guarantee``)。Vector の audit (pipeline_events) で
必要な field (``CODE`` / ``code`` 等) は SAFE_ATTRS に明示列挙すれば
``__str__`` 経由でも公開される。

migration scope: Phase 4 では analysis BC 22 class のみ移行
(``ai_provider_errors`` 10 + ``curation/errors`` 4 + ``assessment/errors`` 5 +
``embedding/errors`` 3)。残り 33 class (collection / external_fetch / search /
ssrf_guard) は notebook 起票 + BC 単位の follow-up PR で順次 migration。

設置場所: Phase 1 の ``app/logfire_setup.py`` と並列の observability 名前空間。
``app/shared/exceptions.py`` ではなく ``app/logfire_*.py`` prefix を採用し、
「Logfire SaaS への PII 流出抑制」が存在理由であることを module path で示す。
"""

from __future__ import annotations

from typing import ClassVar


class VectorDomainError(Exception):
    """Vector domain exception の基底。``__str__`` は class name + SAFE_ATTRS のみ。

    subclass は ``SAFE_ATTRS: ClassVar[tuple[str, ...]]`` で「``__str__``
    で公開して安全な field 名」を宣言する。declarable な field は **PII を
    含まない静的識別子** (CODE / code / stage / status 等) のみ。raw_url /
    message / provider_response 等の PII 含有 field を SAFE_ATTRS に列挙
    してはならない。

    constructor 引数を本 class は明示的には受け取らない (``Exception`` の
    既定 signature をそのまま継承する)。subclass 側で kwargs-only に絞るか、
    *args/**kwargs accept-and-discard で legacy 互換を取るかは subclass の
    設計判断に委ねる。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ()
    """``__str__`` で公開して安全な field 名 (PII を含まない)。subclass が宣言する。"""

    def __str__(self) -> str:
        attrs = {name: getattr(self, name, None) for name in self.SAFE_ATTRS}
        if not attrs:
            return self.__class__.__name__
        attr_str = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
        return f"{self.__class__.__name__}({attr_str})"
