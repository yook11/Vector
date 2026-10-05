"use server";

import { z } from "zod";
import { getArticleListRevision } from "@/lib/cache/article-list-revision";
import { CursorSchema } from "@/lib/validation/cursor";
import type { ArticleQuery } from "@/types";
import type { AnalyzedArticlePreviewList } from "@/types/types.gen";
import { CATEGORY_SLUG_PATTERN } from "../search-params";
import { getArticles } from "./get-articles";

const CategorySchema = z.string().regex(CATEGORY_SLUG_PATTERN).nullable();

/** 記事一覧の続きを読み込む (Server Action)。category は呼び出し側で bind する。 */
export async function loadMoreArticles(
  category: string | null,
  cursor: string,
): Promise<AnalyzedArticlePreviewList> {
  const validCategory = CategorySchema.parse(category);
  const validCursor = CursorSchema.parse(cursor);
  const query: ArticleQuery =
    validCategory === null
      ? { cursor: validCursor }
      : { category: validCategory, cursor: validCursor };
  return getArticles(query, getArticleListRevision());
}
