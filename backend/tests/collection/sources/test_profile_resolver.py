"""``RegistryCompletionProfileResolver`` の profile 解決テスト (DB 非依存)。

P2-D で ``SOURCES`` は ``SourceName → ArticleSource`` (= Source クラス
オブジェクト) になり、resolver は ``SOURCES.get(SourceName(...))`` で引き
``.completion_profile`` をクラス属性として直読みする (``collect`` 非呼出。
``make_adapter`` / ``adapter_factory`` 不在で class-ref 構造保証 = 無
instantiation 契約)。

``source_name`` を渡す経路は DB (``news_sources`` 引き) を一切触らないため
session 不要で unit 検証できる。本テストは「登録ソースは自身の profile を、
未登録は ``DEFAULT_PROFILE`` を返す」業務不変条件を固定する (resolver の
profile 解決は P2 で挙動が registry 構造に依存するようになったため新規 pin)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
)
from app.collection.sources.profile_resolver import (
    RegistryCompletionProfileResolver,
)
from app.shared.value_objects.source_name import SourceName


def _resolver() -> RegistryCompletionProfileResolver:
    # source_name を渡す経路は session を触らないため Mock で十分。
    return RegistryCompletionProfileResolver(MagicMock())


@pytest.mark.asyncio
async def test_registered_default_source_resolves_default_profile() -> None:
    profile = await _resolver().resolve(source_id=1, source_name=SourceName("NASA"))
    assert profile is DEFAULT_PROFILE


@pytest.mark.asyncio
async def test_registered_html_title_source_resolves_html_title_profile() -> None:
    """Anthropic は HTML_TITLE_PROFILE (title=html_preferred) を引く。"""
    profile = await _resolver().resolve(
        source_id=1, source_name=SourceName("Anthropic")
    )
    assert profile is HTML_TITLE_PROFILE


@pytest.mark.asyncio
async def test_unregistered_source_falls_back_to_default_profile() -> None:
    profile = await _resolver().resolve(
        source_id=999, source_name=SourceName("Nonexistent Source")
    )
    assert profile is DEFAULT_PROFILE
