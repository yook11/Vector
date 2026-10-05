/**
 * Type re-exports for narrowing / alias 集約点。
 *
 * Schema types は `backend/app/schemas/` の Pydantic から `@hey-api/openapi-ts`
 * 経由で `types.gen.ts` に自動生成される。`npm run generate-types` で再生成。
 *
 * 本ファイルの責務:
 * - StripNull narrowing: backend が optional + nullable で表現するキーを frontend
 *   側で `null` を剥がして optional のみに揃える
 *
 * 単純 re-export (AnalyzedArticlePreview / AnalyzedArticle / NewsSourceDetail 等) は本ファイル
 * から撤廃済 (PR-H3)。利用側は `@/types/types.gen` から直接 import する。
 */
import type { ListArticlesData } from "@/types/types.gen";

// ---------------------------------------------------------------------------
// StripNull narrowing
// ---------------------------------------------------------------------------

type StripNull<T> = { [K in keyof T]: Exclude<T[K], null> };

/** Query parameters for GET /articles (article listing). */
export type ArticleQuery = StripNull<NonNullable<ListArticlesData["query"]>>;
