const PLACEHOLDER_COUNT = 6;

const bar =
  "rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_9%,transparent)]";

/**
 * カテゴリ・並び替えの再取得中に出すプレースホルダ。
 * DashboardPaperArticleList と同じ 2 カラムグリッドで PaperArticleCard の骨格を写す。
 */
export function DashboardArticleListSkeleton({
  label = "記事を更新中…",
}: {
  label?: string;
}) {
  return (
    <div>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="mb-5 text-sm font-medium text-[var(--vector-ink-soft)]"
      >
        {label}
      </p>
      <div
        aria-hidden="true"
        className="grid grid-cols-1 gap-x-12 gap-y-[30px] md:grid-cols-2"
      >
        {Array.from({ length: PLACEHOLDER_COUNT }).map((_, index) => (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: static skeleton list
            key={index}
            className="flex animate-pulse motion-reduce:animate-none flex-col border-b border-[color-mix(in_oklab,var(--vector-ink)_14%,transparent)] pb-6"
          >
            <div className={`mb-3.5 h-3 w-24 ${bar}`} />
            <div className="mb-3.5 space-y-2.5">
              <div className={`h-5 w-full ${bar}`} />
              <div className={`h-5 w-4/5 ${bar}`} />
            </div>
            <div
              className={`mb-[15px] h-[2.5px] w-[34px] rounded-[2px] ${bar}`}
            />
            <div className="mb-4 space-y-2">
              <div className={`h-3 w-full ${bar}`} />
              <div className={`h-3 w-full ${bar}`} />
              <div className={`h-3 w-3/5 ${bar}`} />
            </div>
            <div className="mt-auto flex items-center justify-between gap-4">
              <div className={`h-3 w-28 ${bar}`} />
              <div className={`h-3 w-16 ${bar}`} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
