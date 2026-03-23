# 値オブジェクト実装の学びと今後の方針

> 作成日: 2026-03-24
> 対象: CategorySlug, CategoryName のプロトタイプ実装

## 1. 実装で判明したこと

### Python + Pydantic での値オブジェクトの複雑さ

Python は動的型付け言語であり、値オブジェクトに必要な以下の機能を言語が提供しない:

| 機能 | 静的型付け言語（例: Kotlin） | Python |
|------|---------------------------|--------|
| 不変性 | `value class` で言語レベル保証 | `__slots__` + `__setattr__` で手動実装 |
| 等値性・ハッシュ | `value class` が自動提供 | `__eq__`, `__hash__` を手動実装 |
| 型制約 | 型システムで表現可能 | ランタイムバリデーションが必要 |

この上に Pydantic（ランタイム型検査フレームワーク）との統合が加わる:

| 統合ポイント | 必要な実装 | 目的 |
|------------|-----------|------|
| `__get_pydantic_core_schema__` | バリデーションパイプライン定義 | Pydantic スキーマのフィールドとして使う |
| `__get_pydantic_json_schema__` | OpenAPI スキーマ制御 | `type: string` として出力する |
| `when_used="always"` | シリアライズモード指定 | `model_dump()` でもプリミティブに変換する |

**結論**: 「言語が提供しない機能の手動実装」+「フレームワーク統合」の二重コストが発生する。

### `to_string_ser_schema()` の罠

`to_string_ser_schema()` のデフォルト `when_used` は `"json-unless-none"`。
これは `model_dump_json()` では効くが、`model_dump()`（Python dict モード）では効かない。

```python
# NG: model_dump() で CategorySlug オブジェクトがそのまま返る
serialization=core_schema.to_string_ser_schema()

# OK: 全モードでプリミティブ文字列に変換される
serialization=core_schema.to_string_ser_schema(when_used="always")
```

### 値オブジェクトの Pydantic 統合パターン（確立済み）

以下のパターンは全値オブジェクトで再利用可能:

```python
@classmethod
def __get_pydantic_core_schema__(cls, source_type, handler):
    def validate(value: str) -> SomeValueObject:
        return cls(value)

    from_str = core_schema.chain_schema([
        core_schema.str_schema(),
        core_schema.no_info_plain_validator_function(validate),
    ])

    return core_schema.json_or_python_schema(
        json_schema=from_str,
        python_schema=core_schema.union_schema([
            core_schema.is_instance_schema(cls),
            from_str,
        ]),
        serialization=core_schema.to_string_ser_schema(when_used="always"),
    )

@classmethod
def __get_pydantic_json_schema__(cls, _core_schema, handler):
    return handler(core_schema.str_schema())
```

## 2. 今後の方針: 値オブジェクト vs Annotated 型の使い分け

### 判断基準

「実装コストが高いから避ける」ではなく「この型に値オブジェクトの意味があるか」で判断する。
Pydantic 統合のボイラープレートは初回のみのコスト（パターンは確立済みでコピー可能）であり、判断材料にしない。

| 問い | Yes → 値オブジェクト | No → Annotated 型 |
|------|-------------------|-----------------|
| この型にイミュータビリティと等値性の保証が要るか？ | クラスで `__eq__`, `__hash__`, `__slots__` を保証 | `Annotated[str, Field(...)]` で十分 |
| この型を dict のキーや set のメンバーとして使うか？ | ハッシュ可能な値オブジェクトが必要 | ただの str で済む |
| 「この型を受け取る」こと自体にドメイン上の意味があるか？ | 型で意図を表現する | バリデーションだけで目的は達成される |

### specs で確定済みの値オブジェクト一覧と実装形式

| 値オブジェクト | 実装形式 | 実装状況 |
|---|---|---|
| `CategorySlug` | クラス | 完了 |
| `CategoryName` | クラス | 完了 |
| `KeywordName` | クラス | 未着手 |
| `NewsSourceName` | クラス | 未着手 |
| `HttpUrl` | `Annotated` 型エイリアス | 未着手 |

### ORM 方針

- **現時点**: SQLModel を継続使用。DB モデルはプリミティブ型のまま
- **将来の方向性**: 複雑なビジネスロジックが絡む部分は SQLAlchemy + `__get_pydantic_core_schema__` に段階的に移行
- **簡単な CRUD**: SQLModel のまま

## 3. 検証結果サマリ

| 検証項目 | 結果 |
|---------|------|
| 値オブジェクトの生成・拒否 | PASS（43テスト） |
| `model_dump()` でプリミティブに変換 | PASS（`when_used="always"` が必要） |
| `model_dump_json()` で文字列出力 | PASS |
| `from_attributes=True` で ORM → スキーマ変換 | PASS |
| OpenAPI スキーマで `type: string` | PASS |
| 既存テスト全パス | PASS（220/220） |
| ruff lint + format | PASS |

## 4. 関連ファイル

| ファイル | 内容 |
|---------|------|
| `backend/app/domain/category.py` | CategorySlug, CategoryName 値オブジェクト |
| `backend/app/domain/__init__.py` | re-export |
| `backend/app/schemas/category.py` | 値オブジェクトを統合したスキーマ |
| `backend/tests/test_domain/test_category_values.py` | 値オブジェクトのテスト（43件） |
| `specs/design-principles.md` | 多層防御・値オブジェクトの設計原則 |
