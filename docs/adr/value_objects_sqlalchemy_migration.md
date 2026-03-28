# 値オブジェクト実装と SQLAlchemy Declarative 移行

## 背景: なぜ SQLModel では DDD の値オブジェクトが実現できないか

Vector プロジェクトでは、バリデーションの一元化を目的として値オブジェクト（VO）を導入している。
VO は「バリデーションルールを型に閉じ込めて、使う側に検証の責任を負わせない」仕組みであり、
スキーマ層・service 層・models 層に同じバリデーションを散らさずに済む。

しかし SQLModel の設計上の制約により、VO を ORM 層に統合することができず、
現在は手書きのボイラープレート（~90行/VO）と手動変換で凌いでいる状態にある。

### SQLModel で不可能なこと

#### 1. カラム型に値オブジェクトを指定できない

SQLModel はカラム定義において Pydantic と SQLAlchemy の両方で動く必要があるため、
基本型（`str`, `int` 等）しか受け付けない。

```python
# SQLModel — これは書けない
class Category(SQLModel, table=True):
    slug: CategorySlug = Field(...)  # カスタム型はエラー

# SQLModel — 書けるのはこれだけ
class Category(SQLModel, table=True):
    slug: str = Field(max_length=50)  # plain str のみ
```

結果として models 層では VO の型情報が失われ、service 層で手動変換が必要になる。

```python
# service 層での手動変換（現在の状態）
category = Category(slug=data.slug.root, name=data.name.root)
```

#### 2. Mapped / mapped_column が使えない

SQLAlchemy 2.0 の新しい型ヒントシステム（`Mapped[T]` + `mapped_column()`）は
SQLModel のクラス定義と互換性がない。
これにより `type_annotation_map` によるグローバルな型 → TypeDecorator 登録も不可能。

#### 3. TypeDecorator は明示的指定のみ

SQLModel でも TypeDecorator 自体は使えるが、`Field(sa_type=CategorySlugType())` という
明示的な指定が必要。`type_annotation_map` の自動解決が効かないため、
VO を使う全カラムに手動で指定しなければならない。

#### 4. composite() が動作しない

SQLModel では composite マッピングが `TypeError: cannot pickle 'property' object` で失敗する
（fastapi/sqlmodel#564、未解決）。
複数カラムにまたがる値オブジェクト（Money, Address 等）を使う場合、致命的な制約となる。

### 現在の VO 実装の問題

上記制約のため、現在の VO は `__get_pydantic_core_schema__` を手書きして
Pydantic との統合を実現している。この実装は 1 VO あたり約 90 行のボイラープレート
（`__eq__`, `__hash__`, `__repr__`, `__setattr__` オーバーライド、Pydantic core schema hooks）
を必要とし、3 つの VO で合計約 270 行になっている。

---

## 採用する方式: RootModel + TypeDecorator + type_annotation_map

### VO 定義層 — Pydantic RootModel

`RootModel[str]` + `frozen=True` で VO を定義する。
現在の手書きボイラープレートは `RootModel` が自動提供するため不要になる。

```python
class CategorySlug(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_]{0,49}$", v):
            raise ValueError("invalid slug format")
        return v
```

RootModel が自動提供するもの:

- 不変性（`frozen=True` → `__setattr__` 禁止 + `__hash__` 生成）
- Pydantic 統合（core schema の自動生成）
- 等価比較（`__eq__`）
- JSON シリアライズ時に生の値として出力（`{"root": "..."}` ではなく `"..."` ）

### ORM 変換層 — SQLAlchemy TypeDecorator

DB の読み書き境界で `str ↔ VO` の変換を自動的に行う。

```python
class CategorySlugType(TypeDecorator):
    impl = String(50)
    cache_ok = True  # クエリキャッシュのため必須

    def process_bind_param(self, value, dialect):
        # Python → DB: VO から文字列を取り出す
        return value.root if isinstance(value, CategorySlug) else value

    def process_result_value(self, value, dialect):
        # DB → Python: 文字列を VO に包む
        return CategorySlug(value) if value else None
```

### models 接続層 — type_annotation_map

DeclarativeBase の `type_annotation_map` に一度登録すれば、
models 層で `Mapped[CategorySlug]` と書くだけで TypeDecorator が自動適用される。

```python
class Base(DeclarativeBase):
    metadata = SQLModel.metadata  # 既存の metadata を共有
    type_annotation_map = {
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        KeywordName: KeywordNameType,
    }

class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[CategorySlug] = mapped_column(unique=True, index=True)
    name: Mapped[CategoryName] = mapped_column(unique=True)
```

### スキーマ層 — 変更なし

スキーマ層はこれまで通り VO 型を書くだけ。RootModel は Pydantic の一部なので
BaseModel のフィールド型にそのまま指定できる。

```python
class CategoryCreate(BaseModel):
    slug: CategorySlug  # これだけでバリデーションが走る
    name: CategoryName
```

---

## SQLModel と DeclarativeBase の共存

### 移行範囲

全テーブルを一括移行するのではなく、VO を使う Category と Keyword だけを
SQLAlchemy Declarative に移行する。他のテーブルは SQLModel のまま共存させる。

### 共存の仕組みと制約

SQLAlchemy は Relationship 解決のために 2 つの仕組みを持つ:

| 仕組み | 用途 | 解決対象 |
|--------|------|----------|
| **metadata** | テーブル定義のカタログ | `ForeignKey("categories.id")` — テーブル名ベース |
| **registry** | Python クラスのカタログ | `relationship("Category")` — クラス名ベース |

SQLModel と DeclarativeBase はそれぞれ独自の registry を持つ。
metadata は共有できるが、registry は別々のまま。

#### 動くもの: ForeignKey 参照（テーブル名ベース）

metadata を共有していれば、SQLModel モデルから DeclarativeBase モデルへの
FK 参照は問題なく動作する。

```python
# SQLModel 側（ArticleKeyword）から DeclarativeBase 側（Keyword）への FK
keyword_id: int = Field(foreign_key="keywords.id")  # テーブル名ベース → OK
```

#### 動かないもの: cross-base Relationship（クラス名ベース）

異なる registry 間で `relationship("ClassName")` は解決できない。

```python
# SQLModel 側から DeclarativeBase 側への Relationship → エラー
keyword: "Keyword" = Relationship(...)  # registry が違うので見つからない
```

### 対処方針

Category / Keyword は依存関係の「葉」であり、他のテーブルから FK で参照される方向が主。
この方向は metadata 共有で動作する。

逆方向（Category/Keyword → 他のモデルへの Relationship）は現状不要。
将来的に必要になった場合は、関連テーブルも DeclarativeBase に移行すればよい。

### Base の設計

```python
# app/models/base.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlmodel import SQLModel

class Base(DeclarativeBase):
    metadata = SQLModel.metadata  # SQLModel の metadata に合わせる
    type_annotation_map = {
        CategorySlug: CategorySlugType,
        CategoryName: CategoryNameType,
        KeywordName: KeywordNameType,
    }
```

`Base.metadata = SQLModel.metadata` の向きが重要:
SQLModel の metadata に DeclarativeBase を合わせることで、
既存の SQLModel テーブル定義と Alembic の `target_metadata` をそのまま使える。

### Alembic への影響

`target_metadata` は既存の `SQLModel.metadata` のままで変更不要。
DeclarativeBase 側のテーブルも同じ metadata に登録されるため、
Alembic の autogenerate が両方のテーブルを検出する。

---

## 移行後の各層の変化

| 観点 | SQLModel（現在） | SQLAlchemy Declarative（移行後） |
|------|------------------|----------------------------------|
| models のカラム型 | `str` しか書けない | `Mapped[CategorySlug]` が書ける |
| VO → DB 変換 | service 層で手動（`.root`） | TypeDecorator で自動 |
| DB → VO 変換 | service 層で手動 | TypeDecorator で自動 |
| type_annotation_map | 使えない | 使える（グローバル登録） |
| VO のボイラープレート | ~90行（手書き core schema） | ~10行（RootModel 継承） |
| composite() | 動作しない | 使える（将来の複合 VO に対応） |

---

## 実装の順序

1. **VO を RootModel ベースに書き直す** — ボイラープレート削減、既存テスト通過確認
2. **TypeDecorator を定義する** — CategorySlugType, CategoryNameType, KeywordNameType
3. **app/models/base.py に DeclarativeBase を定義** — metadata 共有、type_annotation_map 登録
4. **Category モデルを DeclarativeBase に移行** — 最小のテーブルで検証
5. **Keyword モデルを DeclarativeBase に移行** — Category との Relationship 確認
6. **Alembic マイグレーションの動作確認** — autogenerate が正常に動くか検証
7. **service 層の手動変換を削除** — TypeDecorator による自動変換に切り替え

---

## 判断基準のまとめ

### VO を Annotated 型エイリアスにしない理由

`Annotated[str, StringConstraints(...)]` はバリデーションの定義を一元化できるが、
Pydantic のバリデーションコンテキストの外（service 層やパイプライン内部）では
不変条件を強制できない。実体は plain `str` のままであり、
`isinstance` チェックも効かない。

### VO を RootModel にする理由

- 型としての独立性: `CategorySlug` ≠ `str` が実行時にも成立
- Pydantic の外でもバリデーション: `CategorySlug("invalid!")` はどこで呼んでもエラー
- カスタムメソッド追加可能: 将来的にドメイン操作を型に閉じ込められる
- ボイラープレート最小: RootModel が Pydantic 統合を自動提供

### SQLAlchemy Declarative に移行する理由

- TypeDecorator + type_annotation_map で VO の自動変換が可能
- models 層に `Mapped[CategorySlug]` と書けるため型情報が保たれる
- composite() が動作するため、将来の複合 VO にも対応可能
- SQLModel の「単一クラスで domain と persistence を兼ねる」設計が DDD の関心の分離と相反する