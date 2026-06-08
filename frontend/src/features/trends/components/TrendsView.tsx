import type { Trends } from "@/types";
import { CategorySection } from "./CategorySection";
import { TrendsMasthead } from "./TrendsMasthead";

interface TrendsViewProps {
  data: Trends;
}

/**
 * Trends データを紙面意匠で描画する純表示コンポーネント。
 * データ取得・認可は page.tsx 側に委ねる。テスト可能な presentational view。
 */
export function TrendsView({ data }: TrendsViewProps) {
  return (
    <div className="relative z-10 px-5 py-8 sm:px-8 lg:px-10 max-w-[1100px] mx-auto">
      <TrendsMasthead data={data} />

      <div className="flex flex-col gap-12">
        {data.categoryTrends.map((category) => (
          <CategorySection key={category.categoryId} category={category} />
        ))}
      </div>
    </div>
  );
}
