"""ExtractedEntity / EntityRawType VO の不変条件テスト (Phase 1B α-1 Step 1)。

新スキーマで Stage 1 観察台帳に保持する 1 行を表現する VO:
- ``EntityRawType``: 観察用の type ラベル (1-30 字、casing 保持、lower 化しない)
  - β の canonical_type と衝突させないため `match_key` を持たず、casing をそのまま保持
- ``ExtractedEntity``: surface (= EntityName) + raw_type の複合 VO

設計判断:
- ``EntitySurface`` は新規 VO にせず ``EntityName`` を type alias で再利用
  (NFKC + 空白整形 + 200 字 + casing 保持 + match_key 完全一致)
- ``EntityRawType`` の上限 30 字は Stage 1 観察用のラベルとしての実態に合わせた
  (β で canonical_type に正規化するので Stage 1 ラベル長は短くて良い)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.extraction.domain.entity import (
    EntityRawType,
    EntitySurface,
    ExtractedEntity,
)


class TestEntityRawTypeNormalization:
    """NFKC + 空白整形 + casing 保持。lower 化はしない。"""

    def test_preserves_casing(self) -> None:
        """大文字小文字は変更しない (Company はそのまま、company に下げない)。"""
        assert EntityRawType("Company").root == "Company"

    def test_preserves_mixed_casing(self) -> None:
        """混在 casing もそのまま保持する。"""
        assert EntityRawType("AI Model").root == "AI Model"
        assert EntityRawType("OpenSource").root == "OpenSource"

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert EntityRawType("  product  ").root == "product"

    def test_collapses_internal_whitespace(self) -> None:
        """連続する空白は単一空白に統合する。"""
        assert EntityRawType("AI  model").root == "AI model"

    def test_collapses_mixed_whitespace_chars(self) -> None:
        """タブ・改行・全角空白などの連続も単一半角空白に統合する。"""
        assert EntityRawType("AI\t\tmodel").root == "AI model"

    def test_nfkc_full_width_to_half_width(self) -> None:
        """NFKC により全角英数は半角に正規化される。"""
        assert EntityRawType("ＡＰＰ").root == "APP"


class TestEntityRawTypeInvariants:
    """1-30 文字、空文字 reject、frozen。match_key を持たない。"""

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            EntityRawType("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            EntityRawType("   ")

    def test_rejects_over_30_after_normalize(self) -> None:
        """正規化後の長さで 30 を超えるものは reject。"""
        with pytest.raises(ValidationError, match="at most 30"):
            EntityRawType("a" * 31)

    def test_accepts_30_after_normalize(self) -> None:
        """正規化後ちょうど 30 文字は許容。"""
        v = EntityRawType("a" * 30)
        assert len(v.root) == 30

    def test_accepts_one_char(self) -> None:
        assert EntityRawType("a").root == "a"

    def test_immutable(self) -> None:
        v = EntityRawType("Company")
        with pytest.raises(ValidationError, match="frozen"):
            v.root = "hacked"  # type: ignore[misc]

    def test_does_not_have_match_key(self) -> None:
        """match_key プロパティを持たないことで、 EntityType と差別化する。

        β の canonical_type 集計と衝突させない設計。raw_type は casing 保持で
        group_by するため、match_key (lower 化) を露出させない。
        """
        v = EntityRawType("Company")
        assert not hasattr(v, "match_key")


class TestEntitySurfaceAlias:
    """EntitySurface は EntityName の type alias であることを確認。"""

    def test_surface_is_entity_name(self) -> None:
        """EntitySurface は EntityName と同じ型を指す。"""
        from app.analysis.domain.value_objects.entity import EntityName

        assert EntitySurface is EntityName

    def test_surface_normalizes_like_entity_name(self) -> None:
        """surface は EntityName の不変条件 (NFKC + 空白整形 + casing 保持) を継承。"""
        s = EntitySurface("  NVIDIA  ")
        assert s.root == "NVIDIA"
        assert s.match_key == "nvidia"


class TestExtractedEntity:
    """surface + raw_type の複合 VO。"""

    def test_construct(self) -> None:
        e = ExtractedEntity(
            surface=EntitySurface("OpenAI"),
            raw_type=EntityRawType("Company"),
        )
        assert e.surface.root == "OpenAI"
        assert e.raw_type.root == "Company"

    def test_immutable(self) -> None:
        """frozen=True により mutation を拒否する。"""
        e = ExtractedEntity(
            surface=EntitySurface("OpenAI"),
            raw_type=EntityRawType("Company"),
        )
        with pytest.raises(ValidationError, match="frozen"):
            e.surface = EntitySurface("Anthropic")  # type: ignore[misc]

    def test_rejects_invalid_surface(self) -> None:
        """surface が EntityName 不変条件を満たさないとき reject。"""
        with pytest.raises(ValidationError):
            ExtractedEntity(
                surface=EntitySurface(""),
                raw_type=EntityRawType("Company"),
            )

    def test_rejects_invalid_raw_type(self) -> None:
        """raw_type が EntityRawType 不変条件を満たさないとき reject。"""
        with pytest.raises(ValidationError):
            ExtractedEntity(
                surface=EntitySurface("OpenAI"),
                raw_type=EntityRawType(""),
            )


class TestExtractedEntityDedupKey:
    """dedup_key は surface.match_key + raw_type.root のタプル。"""

    def test_dedup_key_uses_surface_match_key(self) -> None:
        """surface 側は lower 化された match_key を返す。"""
        e = ExtractedEntity(
            surface=EntitySurface("NVIDIA"),
            raw_type=EntityRawType("Company"),
        )
        assert e.dedup_key() == ("nvidia", "Company")

    def test_dedup_key_preserves_raw_type_casing(self) -> None:
        """raw_type 側は casing 保持。Company と company は別 key。"""
        e_upper = ExtractedEntity(
            surface=EntitySurface("OpenAI"),
            raw_type=EntityRawType("Company"),
        )
        e_lower = ExtractedEntity(
            surface=EntitySurface("OpenAI"),
            raw_type=EntityRawType("company"),
        )
        assert e_upper.dedup_key() != e_lower.dedup_key()

    def test_dedup_key_collapses_surface_casing(self) -> None:
        """surface の casing 違いは同じ dedup_key を返す (NVIDIA == nvidia)。"""
        e_upper = ExtractedEntity(
            surface=EntitySurface("NVIDIA"),
            raw_type=EntityRawType("Company"),
        )
        e_lower = ExtractedEntity(
            surface=EntitySurface("nvidia"),
            raw_type=EntityRawType("Company"),
        )
        assert e_upper.dedup_key() == e_lower.dedup_key()
