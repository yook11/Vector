import type { BriefingListViewModel } from "../page-models/briefing-list";
import { BriefingBandCard } from "./BriefingBandCard";
import { BriefingMasthead } from "./BriefingMasthead";
import { BriefingPendingRow } from "./BriefingPendingRow";

interface BriefingIndexViewProps {
  data: BriefingListViewModel;
}

/**
 * Briefing 一覧ページの orchestrator view。
 * データ取得・認可は page.tsx 側に委ねる。テスト可能な presentational view。
 */
export function BriefingIndexView({ data }: BriefingIndexViewProps) {
  return (
    <div className="relative z-10 mx-auto max-w-[1180px] px-[clamp(18px,4vw,40px)] pt-[30px] pb-[80px]">
      <BriefingMasthead
        weekStart={data.weekStart}
        weekEnd={data.weekEnd}
        totalArticles={data.totalArticles}
      />

      <div className="mt-2 flex flex-col gap-[16px]">
        {data.ready.map((card) => (
          <BriefingBandCard
            key={card.category.slug}
            card={card}
            currentWeekStart={data.weekStart}
          />
        ))}
      </div>

      <BriefingPendingRow pending={data.pending} />
    </div>
  );
}
