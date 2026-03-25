"""Tests for Category value objects (CategorySlug, CategoryName)."""

import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.category import CategoryName, CategorySlug


# ---------------------------------------------------------------------------
# CategorySlug — Unit Tests
# ---------------------------------------------------------------------------
class TestCategorySlug:
    def test_valid_slug(self) -> None:
        slug = CategorySlug("ai_ml")
        assert slug.value == "ai_ml"
        assert str(slug) == "ai_ml"

    def test_numeric_start(self) -> None:
        """Digit-first slugs are valid (e.g. 5g_telecom)."""
        slug = CategorySlug("5g_telecom")
        assert slug.value == "5g_telecom"

    def test_single_char(self) -> None:
        slug = CategorySlug("a")
        assert slug.value == "a"

    def test_max_length_50(self) -> None:
        value = "a" * 50
        slug = CategorySlug(value)
        assert slug.value == value

    def test_strips_whitespace(self) -> None:
        slug = CategorySlug("  ai_ml  ")
        assert slug.value == "ai_ml"

    def test_rejects_uppercase(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("AI_ML")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("   ")

    def test_rejects_over_50(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("a" * 51)

    def test_rejects_hyphen(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("ai-ml")

    def test_rejects_space(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("ai ml")

    def test_rejects_underscore_start(self) -> None:
        with pytest.raises(ValueError, match="CategorySlug"):
            CategorySlug("_ai_ml")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError, match="Expected str"):
            CategorySlug(123)  # type: ignore[arg-type]

    def test_equality(self) -> None:
        assert CategorySlug("ai_ml") == CategorySlug("ai_ml")
        assert CategorySlug("ai_ml") != CategorySlug("biotech")

    def test_equality_different_type_returns_not_implemented(self) -> None:
        slug = CategorySlug("ai_ml")
        assert slug.__eq__("ai_ml") is NotImplemented
        assert slug.__eq__(42) is NotImplemented

    def test_hash_consistency(self) -> None:
        a = CategorySlug("ai_ml")
        b = CategorySlug("ai_ml")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        slug = CategorySlug("ai_ml")
        with pytest.raises(AttributeError, match="immutable"):
            slug._value = "hacked"  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            slug.anything = "hacked"  # type: ignore[attr-defined]

    def test_repr(self) -> None:
        assert repr(CategorySlug("ai_ml")) == "CategorySlug('ai_ml')"


# ---------------------------------------------------------------------------
# CategoryName — Unit Tests
# ---------------------------------------------------------------------------
class TestCategoryName:
    def test_valid_japanese(self) -> None:
        name = CategoryName("AI・ML")
        assert name.value == "AI・ML"
        assert str(name) == "AI・ML"

    def test_valid_ascii(self) -> None:
        name = CategoryName("Semiconductor")
        assert name.value == "Semiconductor"

    def test_valid_with_hyphen(self) -> None:
        name = CategoryName("バイオ-テクノロジー")
        assert name.value == "バイオ-テクノロジー"

    def test_valid_with_space(self) -> None:
        name = CategoryName("素材 材料")
        assert name.value == "素材 材料"

    def test_max_length_50(self) -> None:
        value = "あ" * 50
        name = CategoryName(value)
        assert name.value == value

    def test_strips_whitespace(self) -> None:
        name = CategoryName("  AI・ML  ")
        assert name.value == "AI・ML"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CategoryName("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CategoryName("   ")

    def test_rejects_over_50(self) -> None:
        with pytest.raises(ValueError, match="at most 50"):
            CategoryName("あ" * 51)

    def test_rejects_special_chars(self) -> None:
        with pytest.raises(ValueError, match="CategoryName"):
            CategoryName("AI<script>")

    def test_rejects_angle_brackets(self) -> None:
        with pytest.raises(ValueError, match="CategoryName"):
            CategoryName("<b>bold</b>")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError, match="Expected str"):
            CategoryName(123)  # type: ignore[arg-type]

    def test_equality(self) -> None:
        assert CategoryName("AI・ML") == CategoryName("AI・ML")
        assert CategoryName("AI・ML") != CategoryName("半導体")

    def test_equality_different_type_returns_not_implemented(self) -> None:
        name = CategoryName("AI・ML")
        assert name.__eq__("AI・ML") is NotImplemented
        assert name.__eq__(42) is NotImplemented

    def test_hash_consistency(self) -> None:
        a = CategoryName("AI・ML")
        b = CategoryName("AI・ML")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        name = CategoryName("AI・ML")
        with pytest.raises(AttributeError, match="immutable"):
            name._value = "hacked"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert repr(CategoryName("AI・ML")) == "CategoryName('AI・ML')"


# ---------------------------------------------------------------------------
# Pydantic Integration Tests
# ---------------------------------------------------------------------------
class TestPydanticIntegration:
    """Verify value objects work as Pydantic model fields."""

    class SampleModel(BaseModel):
        slug: CategorySlug
        name: CategoryName

    def test_model_from_str(self) -> None:
        """String inputs are validated and converted to value objects."""
        m = self.SampleModel(slug="ai_ml", name="AI・ML")
        assert isinstance(m.slug, CategorySlug)
        assert isinstance(m.name, CategoryName)
        assert m.slug.value == "ai_ml"
        assert m.name.value == "AI・ML"

    def test_model_from_value_object(self) -> None:
        """Value object inputs are accepted as-is."""
        slug = CategorySlug("ai_ml")
        name = CategoryName("AI・ML")
        m = self.SampleModel(slug=slug, name=name)
        assert m.slug is slug
        assert m.name is name

    def test_model_dump_unwraps_to_str(self) -> None:
        """model_dump() returns plain strings, not nested objects."""
        m = self.SampleModel(slug="ai_ml", name="AI・ML")
        data = m.model_dump()
        assert data == {"slug": "ai_ml", "name": "AI・ML"}
        assert isinstance(data["slug"], str)
        assert isinstance(data["name"], str)

    def test_model_dump_json(self) -> None:
        """JSON serialization produces flat strings."""
        m = self.SampleModel(slug="ai_ml", name="AI・ML")
        data = json.loads(m.model_dump_json())
        assert data["slug"] == "ai_ml"
        assert data["name"] == "AI・ML"

    def test_model_rejects_invalid_slug(self) -> None:
        with pytest.raises(ValidationError):
            self.SampleModel(slug="INVALID", name="AI・ML")

    def test_model_rejects_invalid_name(self) -> None:
        with pytest.raises(ValidationError):
            self.SampleModel(slug="ai_ml", name="<script>alert(1)</script>")

    def test_json_schema_is_string_type(self) -> None:
        """OpenAPI / JSON Schema should show type: string."""
        schema = self.SampleModel.model_json_schema()
        assert schema["properties"]["slug"]["type"] == "string"
        assert schema["properties"]["name"]["type"] == "string"

    def test_from_attributes(self) -> None:
        """Simulate ORM → schema conversion with from_attributes=True."""

        class OrmLike:
            """Mimics a SQLModel row with plain str attributes."""

            def __init__(self, slug: str, name: str) -> None:
                self.slug = slug
                self.name = name

        class ModelWithFromAttributes(BaseModel):
            model_config = ConfigDict(from_attributes=True)
            slug: CategorySlug
            name: CategoryName

        orm_obj = OrmLike(slug="ai_ml", name="AI・ML")
        m = ModelWithFromAttributes.model_validate(orm_obj)
        assert isinstance(m.slug, CategorySlug)
        assert isinstance(m.name, CategoryName)
        assert m.slug.value == "ai_ml"
        assert m.name.value == "AI・ML"
