"""Category 値オブジェクト (CategorySlug, CategoryName) のテスト。"""

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
        assert slug.root == "ai_ml"
        assert slug.value == "ai_ml"  # 後方互換用
        assert str(slug) == "ai_ml"

    def test_numeric_start(self) -> None:
        """数字始まりの slug は有効 (例: 5g_telecom)。"""
        slug = CategorySlug("5g_telecom")
        assert slug.root == "5g_telecom"

    def test_single_char(self) -> None:
        slug = CategorySlug("a")
        assert slug.root == "a"

    def test_max_length_50(self) -> None:
        value = "a" * 50
        slug = CategorySlug(value)
        assert slug.root == value

    def test_strips_whitespace(self) -> None:
        slug = CategorySlug("  ai_ml  ")
        assert slug.root == "ai_ml"

    def test_rejects_uppercase(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("AI_ML")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("   ")

    def test_rejects_over_50(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("a" * 51)

    def test_rejects_hyphen(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("ai-ml")

    def test_rejects_space(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("ai ml")

    def test_rejects_underscore_start(self) -> None:
        with pytest.raises(ValidationError, match="Category slug"):
            CategorySlug("_ai_ml")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValidationError):
            CategorySlug(123)  # type: ignore[arg-type]

    def test_equality(self) -> None:
        assert CategorySlug("ai_ml") == CategorySlug("ai_ml")
        assert CategorySlug("ai_ml") != CategorySlug("biotech")

    def test_equality_different_type(self) -> None:
        slug = CategorySlug("ai_ml")
        assert slug != "ai_ml"
        assert slug != 42

    def test_hash_consistency(self) -> None:
        a = CategorySlug("ai_ml")
        b = CategorySlug("ai_ml")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        slug = CategorySlug("ai_ml")
        with pytest.raises(ValidationError, match="frozen"):
            slug.root = "hacked"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert repr(CategorySlug("ai_ml")) == "CategorySlug('ai_ml')"


# ---------------------------------------------------------------------------
# CategoryName — Unit Tests
# ---------------------------------------------------------------------------
class TestCategoryName:
    def test_valid_japanese(self) -> None:
        name = CategoryName("AI・ML")
        assert name.root == "AI・ML"
        assert name.value == "AI・ML"  # 後方互換用
        assert str(name) == "AI・ML"

    def test_valid_ascii(self) -> None:
        name = CategoryName("Semiconductor")
        assert name.root == "Semiconductor"

    def test_valid_with_hyphen(self) -> None:
        name = CategoryName("バイオ-テクノロジー")
        assert name.root == "バイオ-テクノロジー"

    def test_valid_with_space(self) -> None:
        name = CategoryName("素材 材料")
        assert name.root == "素材 材料"

    def test_max_length_50(self) -> None:
        value = "あ" * 50
        name = CategoryName(value)
        assert name.root == value

    def test_strips_whitespace(self) -> None:
        name = CategoryName("  AI・ML  ")
        assert name.root == "AI・ML"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            CategoryName("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            CategoryName("   ")

    def test_rejects_over_50(self) -> None:
        with pytest.raises(ValidationError, match="at most 50"):
            CategoryName("あ" * 51)

    def test_rejects_special_chars(self) -> None:
        with pytest.raises(ValidationError, match="CategoryName"):
            CategoryName("AI<script>")

    def test_rejects_angle_brackets(self) -> None:
        with pytest.raises(ValidationError, match="CategoryName"):
            CategoryName("<b>bold</b>")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValidationError):
            CategoryName(123)  # type: ignore[arg-type]

    def test_equality(self) -> None:
        assert CategoryName("AI・ML") == CategoryName("AI・ML")
        assert CategoryName("AI・ML") != CategoryName("半導体")

    def test_equality_different_type(self) -> None:
        name = CategoryName("AI・ML")
        assert name != "AI・ML"
        assert name != 42

    def test_hash_consistency(self) -> None:
        a = CategoryName("AI・ML")
        b = CategoryName("AI・ML")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        name = CategoryName("AI・ML")
        with pytest.raises(ValidationError, match="frozen"):
            name.root = "hacked"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert repr(CategoryName("AI・ML")) == "CategoryName('AI・ML')"


# ---------------------------------------------------------------------------
# Pydantic Integration Tests
# ---------------------------------------------------------------------------
class TestPydanticIntegration:
    """値オブジェクトが Pydantic モデルのフィールドとして機能することを確認する。"""

    class SampleModel(BaseModel):
        slug: CategorySlug
        name: CategoryName

    def test_model_from_str(self) -> None:
        """文字列入力はバリデートされ値オブジェクトへ変換される。"""
        m = self.SampleModel(slug="ai_ml", name="AI・ML")
        assert isinstance(m.slug, CategorySlug)
        assert isinstance(m.name, CategoryName)
        assert m.slug.root == "ai_ml"
        assert m.name.root == "AI・ML"

    def test_model_from_value_object(self) -> None:
        """値オブジェクトを入力するとそのまま受け入れられる。"""
        slug = CategorySlug("ai_ml")
        name = CategoryName("AI・ML")
        m = self.SampleModel(slug=slug, name=name)
        assert isinstance(m.slug, CategorySlug)
        assert isinstance(m.name, CategoryName)

    def test_model_dump_unwraps_to_str(self) -> None:
        """model_dump() はネストしたオブジェクトではなく素の文字列を返す。"""
        m = self.SampleModel(slug="ai_ml", name="AI・ML")
        data = m.model_dump()
        assert data == {"slug": "ai_ml", "name": "AI・ML"}
        assert isinstance(data["slug"], str)
        assert isinstance(data["name"], str)

    def test_model_dump_json(self) -> None:
        """JSON シリアライズはフラットな文字列を生成する。"""
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
        """OpenAPI / JSON Schema では type: string と表示されるべき。"""
        schema = self.SampleModel.model_json_schema()
        # RootModel は $ref + $defs を生成するので解決後の型を検証
        assert schema["$defs"]["CategorySlug"]["type"] == "string"
        assert schema["$defs"]["CategoryName"]["type"] == "string"

    def test_from_attributes(self) -> None:
        """from_attributes=True での ORM → schema 変換をシミュレートする。"""

        class OrmLike:
            """素の str 属性を持つ SQLModel 行を模したクラス。"""

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
        assert m.slug.root == "ai_ml"
        assert m.name.root == "AI・ML"
