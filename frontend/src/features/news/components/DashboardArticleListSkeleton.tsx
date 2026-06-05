const PLACEHOLDER_COUNT = 6;

const bar =
  "rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_9%,transparent)]";

/**
 * カテゴリ・並び替えの再取得中に出すプレースホルダ。
 * DashboardPaperArticleList と同じ 2 カラムグリッドで PaperArticleCard の骨格を写す。
 */
export function DashboardArticleListSkeleton() {
  return (
    <div>
      <span role="status" aria-live="polite" className="sr-only">
        記事を読み込み中
      </span>
      <div
        aria-hidden="true"
        className="grid grid-cols-1 gap-x-12 gap-y-6 md:grid-cols-2"
      >
        {Array.from({ length: PLACEHOLDER_COUNT }).map((_, index) => (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            key={index}
            className="flex animate-pulse flex-col border-b border-[color-mix(in_oklab,var(--vector-ink)_13%,transparent)] pb-5"
          >
            <div className={`mb-3.5 h-3 w-24 ${bar}`} />
            <div className="space-y-2.5 border-b border-[color-mix(in_oklab,var(--vector-ink)_12%,transparent)] pb-3">
              <div className={`h-5 w-full ${bar}`} />
              <div className={`h-5 w-4/5 ${bar}`} />
            </div>
            <div className="mt-3 space-y-2">
              <div className={`h-3 w-full ${bar}`} />
              <div className={`h-3 w-full ${bar}`} />
              <div className={`h-3 w-3/5 ${bar}`} />
            </div>
            <div className="mt-auto flex items-center justify-between gap-4 pt-4">
              <div className={`h-3 w-28 ${bar}`} />
              <div className={`h-3 w-16 ${bar}`} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
