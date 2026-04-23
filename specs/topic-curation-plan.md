# 実装プラン: トピック分類体系のキュレーションとリセット

> ステータス: 議論完了、実装着手可（2026-04-23）
> 想定ブランチ: main から新ブランチ（例 `refactor/topic-curation-reset`）
> 着手前の Alembic head: `640eb6c829eb`

## 背景

現状 DB: `topics` 161 件 / `article_analyses` 351 件。1 topic あたり平均 2.18 記事、**161 件中 92 件（57%）が単発記事のみを持つ**。原因は AI プロンプトの粒度ガイドが弱く、`ai agent launch` / `ai agent acquisition` / `ai agent debugging` のように同一主題が動詞・修飾語の差で細分化されること。さらに security カテゴリでは `software vulnerability` (16) などの攻撃事例ニュースがカテゴリ定義（「サイバー攻撃事例は新しい防御技術に関する場合のみ」）に反して大量流入している。

このプランは、分類体系を「**業界視点・主題（subject）中心の集約**」に再設計し、既存データを完全リセットして新ルールで再分類する。

## 設計方針（議論の確定事項）

| # | 確定事項 | 内容 |
|---|---|---|
| D1 | 集約軸 | **β: 業界視点（技術アプローチ・サブセクター）**。会社名・銘柄群では分けない（`ai agents` で OpenAI / Anthropic / Google を統合） |
| D2 | 細分化抑制 | **subject 中心**。動詞・event-type（launch / acquisition / debugging）では分けない |
| D3 | シードの目的 | (a) AI に「業界標準名」を教える、(b) 粒度を強制する、ではなく **(c) 業界に明らかに存在する代表テーマを必ず存在させる** ことのみ。少数に絞る |
| D4 | 動的生成 | **維持**。シードに無い新領域は AI がガイドラインに従って動的生成 → DB 追加。「カテゴリの手動小分類の限界を打破する」というアプリの本質的価値を保つ |
| D5 | 内部キーは英語 | TopicName 値オブジェクトの ASCII 制約は維持。理由: 正規化・重複検出のしやすさ、業界用語の安定性、AI 生成精度、pgvector 同義性検出の安定 |
| D6 | 表示は日本語 | `topics` に `label_ja` カラム（nullable）を追加。シード 25 個には手動キュレーションでラベル付与。動的生成された topic は当面 NULL（フロントで英語名にフォールバック表示） |
| D7 | AI への日本語生成は要求しない | プロンプトに日本語ラベル生成を追加すると AI のタスクが増え精度懸念がある。一度英語のみで運用し、必要なら後で追加する判断 |
| D8 | 既存データの扱い | **R-1: 完全リセット**。`article_analyses` / `article_rejections` / 既存 161 `topics` を全削除 → シード 25 個投入 → 全 `article_extractions`（351 件）に対して Stage 2 のみ再実行。Stage 1（翻訳・要約・エンティティ）と Stage 3（embedding）は触らない |
| D9 | 再分析の実行形態 | **ワンショットスクリプト**。`backend/scripts/reclassify_all.py` を新規作成し、Docker コンテナ内で 1 回だけ実行 |
| D10 | security の攻撃事例ニュース | 原則 `out_of_scope` に倒す。プロンプトの security カテゴリ説明で「攻撃事例単独は対象外、防御技術が主題のときのみ」を強調 |

## シード 25 個確定リスト

カテゴリ slug 順、英語 `name` + 日本語 `label_ja`。動詞・銘柄名は含まない。

### ai
| name | label_ja |
|---|---|
| llm | 大規模言語モデル |
| ai agents | AIエージェント |

### computing
| name | label_ja |
|---|---|
| quantum computing | 量子コンピューティング |

### bio
| name | label_ja |
|---|---|
| cell therapy | 細胞治療 |
| gene therapy | 遺伝子治療 |
| mrna platforms | mRNAプラットフォーム |

### semiconductor
| name | label_ja |
|---|---|
| lithography | リソグラフィ |
| memory | 半導体メモリ |

### energy
| name | label_ja |
|---|---|
| nuclear fusion | 核融合 |
| small modular reactor | 小型モジュール炉（SMR） |

### materials
| name | label_ja |
|---|---|
| superconductors | 超伝導体 |
| additive manufacturing | アディティブ製造 |

### network
| name | label_ja |
|---|---|
| 6g | 6G |
| open ran | Open RAN |
| satellite internet | 衛星インターネット |

### security
| name | label_ja |
|---|---|
| post-quantum cryptography | 耐量子暗号 |
| ai security | AIセキュリティ |

### space
| name | label_ja |
|---|---|
| launch vehicles | ロケット |
| satellite constellations | 衛星コンステレーション |
| lunar program | 月面プログラム |
| mars exploration | 火星探査 |

### robotics
| name | label_ja |
|---|---|
| humanoid robots | ヒューマノイドロボット |
| autonomous driving | 自動運転 |
| drones | ドローン |
| evtol | eVTOL |

合計: **25 件**（robotics カテゴリ定義に明示列挙された 4 サブセクターをシード化。他の robotics サブセクター（industrial / surgical 等）は記事頻度を見て後付けで追加）

## プロンプト改修

`backend/app/analysis/classifier/gemini.py` の `CLASSIFICATION_PROMPT` を更新する。

### Step 2 の差し替え

旧:
```
Step 2 — topic を決定する。
選んだ category 内で、簡潔な topic ラベルを割り当ててください。ルール:
- 小文字英語、2〜4 語、冠詞（a/an/the）不可
- category 内で確立された用語を使う
- 具体的に: 「semiconductor news」ではなく「euv lithography advancement」のように
- out_of_scope の場合は記事内容を端的に表す自然な topic（例: "celebrity gossip",
  "generic saas release"）で構いません
```

新:
```
Step 2 — topic を決定する。
選んだ category 内で、記事の「主題（subject）」を表す topic ラベルを割り当ててください。

【トピック選択の優先順位】
1. 既存トピックの中に主題が同じものがあれば、必ずそれを再利用する
2. 既存に該当が無い場合のみ、新規トピックを作る
3. 迷ったら既存トピックを選ぶ

【新規トピックを作る場合のルール】
1. 業界で確立されたサブセクター名を使う
   例: 「neuromorphic chip」OK、「nvidia chip launch」NG
2. 会社名・製品名を含めない
   例: 「openai release」NG、「llm」OK
3. 動詞・イベント名を含めない
   例: 「launch」「acquisition」「debugging」「development」を語末に付けない
4. 既存トピックの派生バリエーションを作らない
   例: 「ai agents」が既に存在するときに「ai agent debugging」を作らない
5. 命名形式: 小文字英語、2〜4 語、冠詞（a/an/the）不可

【out_of_scope の場合】
記事内容を端的に表す自然な topic（例: "celebrity gossip", "generic saas release"）
で構いません。同じガイドラインを満たさなくてもよい。
```

### `_build_existing_topics_section` の強化

旧: 上位 30 件をフラット列挙

新:
- カテゴリ別に**全件**列挙（出現回数順、上限なし）
- 「主題が同じなら必ず再利用すること」の明示

```python
def _build_existing_topics_section(
    topics_by_category: dict[str, list[str]] | None,
) -> str:
    """カテゴリ内の既存 Topic リストをプロンプトに挿入する。

    主題が同じなら必ず既存を再利用させるため、上限を設けず全件提示する。
    """
    if not topics_by_category:
        return ""

    lines = [
        "Existing topics by category. "
        "If the article's subject matches any of these, you MUST reuse it. "
        "Only create a new one when the subject is genuinely new.",
    ]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{t}"' for t in topics)
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"
```

### security カテゴリ説明の強調

Step 1 の security 行を以下に差し替え:
```
- security: PQC、コンフィデンシャルコンピューティング、FHE、ZKP、AI セキュリティ
  例: 耐量子暗号標準、ゼロ知識証明システム、AI モデルへの攻撃と防御
  境界: サイバー攻撃事例（脆弱性報告、データ漏洩、ランサムウェア等）は
        新しい防御技術が主題の場合に限る。それ以外は out_of_scope
```

## DB 変更

### Phase 1: Alembic マイグレーション 2 本

#### rev_F: `topics.label_ja` カラム追加

```python
# backend/alembic/versions/<rev_F>_topics_add_label_ja.py
# down_revision = "640eb6c829eb"

def upgrade() -> None:
    op.add_column(
        "topics",
        sa.Column("label_ja", sa.String(200), nullable=True),
    )

def downgrade() -> None:
    op.drop_column("topics", "label_ja")
```

#### rev_G: 既存データクリア + シード投入

```python
# backend/alembic/versions/<rev_G>_topics_reset_and_seed.py
# down_revision = "<rev_F>"

SEED_TOPICS: list[dict[str, str]] = [
    {"name": "llm",                       "label": "大規模言語モデル",        "category_slug": "ai"},
    {"name": "ai agents",                 "label": "AIエージェント",          "category_slug": "ai"},
    {"name": "quantum computing",         "label": "量子コンピューティング",  "category_slug": "computing"},
    {"name": "cell therapy",              "label": "細胞治療",                "category_slug": "bio"},
    {"name": "gene therapy",              "label": "遺伝子治療",              "category_slug": "bio"},
    {"name": "mrna platforms",            "label": "mRNAプラットフォーム",    "category_slug": "bio"},
    {"name": "lithography",               "label": "リソグラフィ",            "category_slug": "semiconductor"},
    {"name": "memory",                    "label": "半導体メモリ",            "category_slug": "semiconductor"},
    {"name": "nuclear fusion",            "label": "核融合",                  "category_slug": "energy"},
    {"name": "small modular reactor",     "label": "小型モジュール炉（SMR）", "category_slug": "energy"},
    {"name": "superconductors",           "label": "超伝導体",                "category_slug": "materials"},
    {"name": "additive manufacturing",    "label": "アディティブ製造",        "category_slug": "materials"},
    {"name": "6g",                        "label": "6G",                      "category_slug": "network"},
    {"name": "open ran",                  "label": "Open RAN",                "category_slug": "network"},
    {"name": "satellite internet",        "label": "衛星インターネット",      "category_slug": "network"},
    {"name": "post-quantum cryptography", "label": "耐量子暗号",              "category_slug": "security"},
    {"name": "ai security",               "label": "AIセキュリティ",          "category_slug": "security"},
    {"name": "launch vehicles",           "label": "ロケット",                "category_slug": "space"},
    {"name": "satellite constellations",  "label": "衛星コンステレーション",  "category_slug": "space"},
    {"name": "lunar program",             "label": "月面プログラム",          "category_slug": "space"},
    {"name": "mars exploration",          "label": "火星探査",                "category_slug": "space"},
    {"name": "humanoid robots",           "label": "ヒューマノイドロボット",  "category_slug": "robotics"},
    {"name": "autonomous driving",        "label": "自動運転",                "category_slug": "robotics"},
    {"name": "drones",                    "label": "ドローン",                "category_slug": "robotics"},
    {"name": "evtol",                     "label": "eVTOL",                   "category_slug": "robotics"},
]


def upgrade() -> None:
    # 削除順: analyses → rejections → topics（FK RESTRICT のため）
    op.execute("DELETE FROM article_analyses;")
    op.execute("DELETE FROM article_rejections;")
    op.execute("DELETE FROM topics;")

    bind = op.get_bind()
    cat_rows = bind.execute(sa.text("SELECT id, slug FROM categories")).fetchall()
    cat_id_by_slug = {row.slug: row.id for row in cat_rows}

    for seed in SEED_TOPICS:
        bind.execute(
            sa.text("""
                INSERT INTO topics (name, category_id, label_ja, created_at)
                VALUES (:name, :cat_id, :label, now())
            """),
            {
                "name": seed["name"],
                "cat_id": cat_id_by_slug[seed["category_slug"]],
                "label": seed["label"],
            },
        )


def downgrade() -> None:
    # 完全リセットのため downgrade で復元はできない
    op.execute("DELETE FROM topics WHERE label_ja IS NOT NULL;")
```

> **注意**: `article_analyses` 削除により `watchlist_entries` が CASCADE 削除される。開発段階のため許容（D8）。本番リリース後に同様のリセットを行う場合は別の手順が必要。

## モデル更新

### Phase 2: `Topic` モデルに `label_ja` 追加

`backend/app/models/topic.py`:
```python
class Topic(Base):
    # 既存フィールドに追加
    label_ja: Mapped[str | None] = mapped_column(String(200), nullable=True)
```

## API レスポンス拡張

### Phase 3: `TopicEmbed` スキーマに `label_ja` 追加

`backend/app/schemas/embeds.py`:
```python
class TopicEmbed(BaseModel):
    name: TopicName
    label_ja: str | None = None
```

→ `npm run generate-types` でフロント型再生成

## フロントエンド表示更新

### Phase 4: トピック表示で日本語ラベルにフォールバック

```typescript
// 表示ロジック共通化
const getTopicLabel = (topic: { name: string; labelJa?: string | null }): string =>
  topic.labelJa ?? topic.name
```

変更対象:
- `frontend/src/components/news/NewsCard.tsx`: Badge 表示を `getTopicLabel(article.topic)` に
- `frontend/src/components/news/NewsDetail.tsx`: 同上
- `frontend/src/components/layout/CategorySidebar.tsx`: トピックリンクのテキストを `getTopicLabel(t)` に

## 再分析ワンショットスクリプト

### Phase 5: `scripts/reclassify_all.py`

```python
"""全 article_extractions に対して Stage 2 のみ再実行する。

Phase 1 のリセットマイグレーション後に 1 度だけ実行する想定。
classify_content タスクをキューに投入し、ワーカーが順次処理する。
"""

import asyncio

from sqlalchemy import select

from app.analysis.tasks import classify_content
from app.db import async_session_factory
from app.models.article_extraction import ArticleExtraction


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(select(ArticleExtraction.article_id))
        article_ids = [row[0] for row in result]

    print(f"enqueueing {len(article_ids)} reclassification tasks")
    for aid in article_ids:
        await classify_content.kiq(aid)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
```

実行手順:
```bash
# 1. リセットマイグレーション適用（停止不要、削除 + 投入のみ）
docker exec vector-backend-1 alembic upgrade head

# 2. ワーカーが動いていることを確認
docker ps --format '{{.Names}}\t{{.Status}}' | grep worker-analysis

# 3. ワンショット投入
docker exec vector-backend-1 python -m scripts.reclassify_all

# 4. ワーカーログを監視（RPM=50 で 7〜10 分）
docker logs -f vector-worker-analysis-1
```

> 既存の Embedding は触らない。`ArticleAnalysis` は新規作成されるが、`embedding` カラムは Stage 3 のチェーンで埋まる（`classify_content` が `classified` を返したら自動的に `generate_embedding.kiq` が走る）ため、結果として **Embedding も全件再生成される**。これは新しい topic 体系に基づいた埋め込みとして正しい挙動。

## 実装ステップ（依存順）

| # | フェーズ | 作業 | 検証 |
|---|---|---|---|
| 1 | Alembic | rev_F（カラム追加）+ rev_G（リセット & シード投入） | `alembic upgrade head` がエラーなく完了、`SELECT count(*) FROM topics` = 25 |
| 2 | Model | `Topic.label_ja` 追加 | `pytest tests/test_models/` |
| 3 | Schema | `TopicEmbed.label_ja` 追加 | `npm run generate-types` 成功 |
| 4 | Classifier | プロンプト改修、`_build_existing_topics_section` 強化 | `pytest tests/test_classifier/` |
| 5 | Frontend | 表示ロジックを日本語ラベル fallback に | `npm run build`、画面で日本語表示確認 |
| 6 | Script | `scripts/reclassify_all.py` 作成 | dry-run（投入件数の確認） |
| 7 | 検証 | `ruff check` + `pytest` | 全件通過 |
| 8 | 実行 | リセット & 再分析実行 | DB の topic 分布、out_of_scope 件数確認 |

## 変更/新規ファイル一覧

### 新規ファイル

- `backend/alembic/versions/<rev_F>_topics_add_label_ja.py`
- `backend/alembic/versions/<rev_G>_topics_reset_and_seed.py`
- `backend/scripts/reclassify_all.py`

### 変更ファイル

- `backend/app/models/topic.py` — `label_ja` カラム追加
- `backend/app/analysis/classifier/gemini.py` — `CLASSIFICATION_PROMPT` 改修、`_build_existing_topics_section` 強化、security カテゴリ説明強調
- `backend/app/schemas/embeds.py` — `TopicEmbed.label_ja` 追加
- `frontend/src/types/generated.ts` — 自動生成（コミット）
- `frontend/src/components/news/NewsCard.tsx` — 表示を `labelJa ?? name` に
- `frontend/src/components/news/NewsDetail.tsx` — 同上
- `frontend/src/components/layout/CategorySidebar.tsx` — 同上

### テスト追加/更新

- `backend/tests/test_classifier/test_gemini_prompt.py` — 新規生成ガイドラインを含むプロンプト構築のスナップショット
- `backend/tests/test_models/test_topic.py` — `label_ja` の nullable 動作確認

## リスクと切り分け

### 破壊的変更

- **データ消失**: `article_analyses` / `article_rejections` / 既存 `topics` を全削除する。**復元不能**。事前に `pg_dump` でバックアップ取得を必須とする
- **`watchlist_entries` の CASCADE 削除**: `article_analyses` 削除に伴いユーザーのウォッチリストが消える。開発段階のため許容
- **API 互換性**: `TopicEmbed` に nullable フィールド追加のみのため後方互換あり

### 再分析中の挙動

- 再分析実行中、`/api/v1/articles` は空に近い結果を返す（analyses が逐次再生成される）
- ユーザー影響を避けたい場合はメンテナンスモードで実行（開発段階のため不要）

### 動的生成の品質懸念

- シードを 21 個に絞ったため、初期は動的生成された topic が多くなる可能性
- プロンプトのガイドライン強化（5 ルール + 全件提示）で細分化を防ぐが、実運用で破綻するリスクは残る
- **対応**: 再分析後に DB の topic 分布を確認し、想定外の細分化があればプロンプトを再調整 → 再リセット（同じワンショットで再実行可能）

### Gemini API コスト・レート制限

- 351 件 × 1 call = 351 calls
- Gemini 2.5 Flash Lite: RPM=50（7 分以上必要）、RPD=1500（余裕）
- コスト: ~$1 未満
- ワーカー側のレートリミッター（Redis 経由）で自動制御されるため、過剰投入は起きない

## 規模見積もり

- マイグレーション 2 本（~80 行）
- モデル変更（~5 行）
- プロンプト改修（~50 行）
- スキーマ変更（~3 行）
- フロントエンド変更（~30 行）
- ワンショットスクリプト（~30 行）
- テスト追加（~100 行）
- **合計**: 約 300 行規模、単一 PR 推奨

## 検証

### コード検証
```bash
docker exec vector-backend-1 uv run ruff check app/ tests/
docker exec vector-backend-1 uv run ruff format --check app/ tests/
docker exec vector-backend-1 uv run pytest tests/ -x -q
docker exec vector-frontend-1 npm run lint
docker exec vector-frontend-1 npm run build
```

### 再分析後の品質チェック
```sql
-- 全体分布
SELECT c.slug, COUNT(DISTINCT t.id) AS topic_count, COUNT(aa.id) AS analysis_count
FROM categories c
LEFT JOIN topics t ON t.category_id = c.id
LEFT JOIN article_analyses aa ON aa.topic_id = t.id
GROUP BY c.slug
ORDER BY analysis_count DESC NULLS LAST;

-- シードと動的生成の比率
SELECT
  COUNT(*) FILTER (WHERE label_ja IS NOT NULL) AS seeded,
  COUNT(*) FILTER (WHERE label_ja IS NULL)     AS dynamic,
  COUNT(*) AS total
FROM topics;

-- out_of_scope 件数（rejected）
SELECT COUNT(*) FROM article_rejections;

-- 単発トピックの数（57% から減ったか）
SELECT COUNT(*) FROM topics t
WHERE NOT EXISTS (
  SELECT 1 FROM article_analyses aa
  WHERE aa.topic_id = t.id GROUP BY aa.topic_id HAVING COUNT(*) > 1
);
```

期待値:
- 単発トピックの比率が **57% → 30% 未満** に減少
- 動的生成された topic のうち、明らかに細分化されたもの（`* launch` `* acquisition` 等）がゼロ
- `article_rejections` に security の攻撃事例ニュースが移動

### 手動 UI 確認
- `/` でトピック Badge が日本語表示されているか
- サイドバーのカテゴリ展開でトピックが日本語で並ぶか
- トピックフィルタ（`?topic=量子コンピューティング`）が機能するか
  - フィルタは英語 `name` ベースなので URL は `?topic=quantum+computing` になる想定。日本語表示は表示時のみ

## 再開時のチェックリスト

別セッションで実装を再開する場合:

1. 本ファイルを通読
2. `git log --oneline -10` で main の最新を確認
3. `docker exec vector-backend-1 alembic current` で head が `640eb6c829eb` であることを確認
4. main から新ブランチ `refactor/topic-curation-reset` を作成
5. Phase 1（Alembic）→ Phase 2（Model）→ Phase 3（Schema）→ Phase 4（Classifier）→ Phase 5（Frontend）→ Phase 6（Script）→ Phase 7（検証）→ Phase 8（実行）の順で進行
6. Phase 4 までは PR レビュー前に検証コマンドを通す
7. Phase 8 の実行は **PR マージ後に本番（=このローカル開発環境）で実行**。再分析の結果を元に必要ならプロンプトを再調整
