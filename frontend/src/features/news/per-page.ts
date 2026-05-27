/**
 * perPage の許容値・default・型判定を集約した server-safe policy module。
 *
 * "use client" を付けないことで Server Component の `search-params.ts` と
 * Client Component の `PerPageSelect.tsx` の両方から安全に import できる。
 * backend `PaginationParams.per_page` default (= 24) と一致させること。
 */

export const PER_PAGE_OPTIONS = ["12", "24", "48", "100"] as const;
export const DEFAULT_PER_PAGE = "24";

export type PerPageOption = (typeof PER_PAGE_OPTIONS)[number];

const PER_PAGE_OPTION_SET = new Set<string>(PER_PAGE_OPTIONS);

export function isPerPageOption(value: string): value is PerPageOption {
  return PER_PAGE_OPTION_SET.has(value);
}
