/**
 * NewsSource 入力スキーマ。
 *
 * SSoT は backend の `app/schemas/news_source.py:NewsSourceCreate` (= `SourceName`
 * + `SafeUrl` + `SourceType`)。frontend の zod は SSoT 完全コピーではなく、UX
 * (入力中のフィールド単位エラー) と defense-in-depth (Server Action 直叩き耐性
 * + URL scheme allowlist) を担う。最終 invariant は backend が保証する。
 *
 * `SOURCE_TYPES` は `as const satisfies readonly NonNullable<SourceType>[]`
 * で固定しており、`SourceType` は `@/types/types.gen` から取得した backend 由来の
 * union (`'rss' | 'api' | 'html'`) を直接参照する。
 */

import { z } from "zod";
import type { SourceType as GeneratedSourceType } from "@/types/types.gen";

const SOURCE_TYPES = [
  "rss",
  "api",
] as const satisfies readonly NonNullable<GeneratedSourceType>[];

export const SourceTypeSchema = z.enum(SOURCE_TYPES);
export type SourceType = z.infer<typeof SourceTypeSchema>;

// SourceName backend invariant (`app/collection/domain/value_objects/source.py`):
// - trim 後 1-50 chars
// - 少なくとも 1 ワード文字を含む (`(?=.*\w)`)
// - 使用可能文字: Unicode ワード文字・空白・ハイフン・ドット・プラス・スラッシュ
// frontend pattern は HTML5 input pattern と異なり JS 正規表現 (u フラグ) で評価
// される (zod 内部) ため日本語等 BMP 外文字も `\w` (Unicode property `\p{L}\p{N}_`)
// で通る。
const SOURCE_NAME_PATTERN = /^(?=.*[\p{L}\p{N}_])[\p{L}\p{N}_ \-./+]+$/u;

const SourceNameSchema = z
  .string()
  .trim()
  .min(1, "Name is required")
  .max(50, "Name must be at most 50 characters")
  .regex(
    SOURCE_NAME_PATTERN,
    "Name can only contain letters, numbers, spaces, hyphens, dots, underscores, plus signs, and slashes",
  );

// SafeUrl backend invariant (`app/shared/value_objects/safe_url.py`):
// - http/https スキームのみ
// - max 2048 chars
// - AnyHttpUrl 構造
// SSRF guard (private IP literal 等の構造的拒否) は backend が担う。frontend は
// scheme allowlist + 長さ + URL 構造までを zod で表現する。
const SafeUrlSchema = z
  .url({ protocol: /^https?$/, error: "Must be a valid http(s) URL" })
  .max(2048, "URL must be at most 2048 characters");

export const NewSourceSchema = z.object({
  name: SourceNameSchema,
  sourceType: SourceTypeSchema,
  siteUrl: SafeUrlSchema,
  endpointUrl: SafeUrlSchema,
});

export type NewSourceInput = z.infer<typeof NewSourceSchema>;
