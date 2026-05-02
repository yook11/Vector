/**
 * Next.js Server Component の `'use cache'` / fetch `next.tags` / Server Action
 * `updateTag` で共通利用する cache tag literal の中央 registry。
 *
 * tag 文字列を const に集約することで、typo 1 文字で invalidation chain が
 * silent に死ぬ事故 (compile error も runtime error も出ない) を構造的に防ぐ。
 *
 * 増設方針:
 * - features/ に新しい resource (cache 単位) を足したら、まずここに entry 追加
 * - 命名規約: `<resource>` または `<resource>:<scope>` (区切り `:` 固定)
 * - `<resource>` は features/ ドメイン単位 (`watchlist`, `sources`, `articles`,
 *   `categories`, `digest` 等)
 * - per-user は `:me`、特定 ID なら `:<id>` を後置 (例: 将来 `articles:42`)
 * - TS const 名は camelCase (`watchlistMe`)、tag literal は colon 区切り
 *   (`"watchlist:me"`)
 *
 * cache 戦略の採用基準:
 * - cookies()/headers() を読まない & response が user 非依存 →
 *   `'use cache'` + `cacheLife(profile)` (+ 必要なら `cacheTag(...)`)
 * - 認証 / per-user → legacy fetch options (`next: { tags: [...] }`) +
 *   Server Action 後の `updateTag(tag)`
 *
 * cacheLife profile (現状採用済み):
 * - "seconds"  : search 結果など UX 上の即応性が必要 (stale 30s/rev 1s/exp 1m)
 * - "minutes"  : 記事一覧 (ingestion 周期 ~30min に対し rev 1min で十分新鮮)
 * - "hours"    : 記事詳細・カテゴリ・類似記事 (低頻度更新)
 * - "days"     : 週次集約 (cron 月曜 00:05、TTL 揺れ吸収のため日単位)
 */
export const cacheTags = {
  watchlistMe: "watchlist:me",
  sources: "sources",
  briefingList: "briefing:list",
} as const;

export type CacheTag = (typeof cacheTags)[keyof typeof cacheTags];

/**
 * カテゴリごとの briefing 詳細 cache tag。slug が dynamic のため registry
 * の literal const には収まらないが、tag literal の組み立てを 1 箇所に集約
 * することで typo を防ぐ。backend notifier (FrontendRevalidateNotifier) は
 * 同じ命名で revalidate を打つ (`backend/app/insights/briefing/application/notifier.py`)。
 */
export function briefingCategoryTag(slug: string): `briefing:${string}` {
  return `briefing:${slug}`;
}
