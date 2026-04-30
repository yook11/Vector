import { z } from "zod";

/**
 * 永続化 ID (positive integer) の zod schema。
 *
 * Server Action は network 越境なので、Client UI を bypass する hostile call
 * (curl 等) で number 以外/0/負数/小数が直接 backend に届きうる。defense-in-depth
 * として Server Action 内で受け取った number 引数を再検証する用。
 */
export const PositiveIdSchema = z.number().int().positive();

/**
 * dynamic route param (`[id]`) を整数 ID に coerce + 検証する用。
 *
 * Next.js の `params` は `string` で渡るため、`z.coerce.number()` で number に
 * 変換した上で `int().positive()` を要求する。`/news/abc` (NaN), `/news/1.5`
 * (小数), `/news/-1` (負数), `/news/0` (非 positive) は全て failure。
 */
export const PositiveIdParamSchema = z.coerce.number().int().positive();
