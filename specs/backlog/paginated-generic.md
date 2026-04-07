# Paginated[T] ジェネリック化

## 動機

現在 `PaginatedArticleResponse` は `ArticleBrief` 専用のページネーションレスポンスクラス。
将来 2 つ目のページネーション型（例: `SourceBrief`, `Keyword`）が必要になった時点で、
フィールド定義と `create` ファクトリがコピペになる。

ジェネリック `Paginated[T]` にすれば、型ごとのクラス定義が不要になる。

## 設計案

```python
from typing import Generic, TypeVar

T = TypeVar("T")

class Paginated(_CamelBase, Generic[T]):
    items: list[T]
    total: int
    page: int
    per_page: int
    total_pages: int

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        pagination: PaginationParams,
    ) -> Paginated[T]:
        return cls(
            items=items,
            total=total,
            page=pagination.page,
            per_page=pagination.per_page,
            total_pages=pagination.total_pages(total),
        )
```

使用例:
```python
async def list_watchlist(...) -> Paginated[ArticleBrief]:
    return Paginated.create(items=..., total=..., pagination=...)
```

## 注意点

- **OpenAPI スキーマ名**: Pydantic v2 が `Paginated_ArticleBrief_` のような名前を自動生成する。フロント型生成に影響がないか確認が必要
- **タイミング**: 2 つ目のページネーション型が必要になった時点で着手。1 種類しかない段階では YAGNI

## 影響範囲

- `schemas/articles.py` — `PaginatedArticleResponse` を `Paginated[ArticleBrief]` に置換（またはエイリアス化）
- `schemas/base.py` — `Paginated[T]` クラスを新設
- 全サービス・ルーターの戻り値型を変更
- フロント型生成の再実行
