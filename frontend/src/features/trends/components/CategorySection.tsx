import { getCategoryKicker } from "@/components/paper";
import type { CategoryTrends } from "@/types";
import { RankingColumn } from "./RankingColumn";

interface CategorySectionProps {
  category: CategoryTrends;
}

/** カテゴリ1節(見出し+2カラムgrid)。 */
export function CategorySection({ category }: CategorySectionProps) {
  const kicker = getCategoryKicker(category.categorySlug);

  return (
    <section aria-label={category.categoryName}>
      {/* 見出し行 */}
      <div className="mb-5 flex items-center gap-3">
        {/* 二色スプリット小四角 */}
        <span
          aria-hidden="true"
          className="shrink-0 size-4 rounded-[2px]"
          style={{
            background: `linear-gradient(135deg, ${kicker.hue} 50%, var(--vector-ink) 50%)`,
          }}
        />
        <h2
          className="text-[clamp(18px,2.4vw,24px)] font-bold leading-tight text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {category.categoryName}
        </h2>
        <span
          className="text-[11px] font-semibold tracking-[0.22em] text-[var(--vector-ink-muted)] italic"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {kicker.code}
        </span>
        {/* 伸ばし罫線 */}
        <div
          className="flex-1 h-px bg-[var(--vector-line)]"
          aria-hidden="true"
        />
      </div>

      {/* 2カラム grid (md未満は1カラム) */}
      <div className="grid gap-0 md:grid-cols-2 md:divide-x md:divide-[var(--vector-line)]">
        <div className="md:pr-6">
          <RankingColumn mode="count" mentions={category.mostMentioned} />
        </div>
        <div className="mt-8 md:mt-0 md:pl-6">
          <RankingColumn mode="growth" mentions={category.fastestGrowing} />
        </div>
      </div>
    </section>
  );
}
