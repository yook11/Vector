import { TrendingDown, TrendingUp } from "lucide-react";
import { formatGrowthRate } from "@/lib/format/percent";

interface GrowthTagProps {
  growthRate: number;
  /** 前週件数が 0 なら新登場扱い(burst 強調)。 */
  previousAppearanceCount: number;
}

/** 伸び率を矢印+%で示す純コンポーネント。新登場(burst)は強調表示。 */
export function GrowthTag({
  growthRate,
  previousAppearanceCount,
}: GrowthTagProps) {
  const isBurst = previousAppearanceCount === 0;
  const isPositive = growthRate >= 0;

  // burst か正成長 → accent-ink、負成長 → 赤系
  const colorStyle =
    isBurst || isPositive ? "var(--vector-accent-ink)" : "#C0556B";

  const Icon = isPositive ? TrendingUp : TrendingDown;

  return (
    <span
      className="inline-flex items-center gap-0.5 tabular-nums font-semibold"
      style={{
        fontFamily: "var(--font-vector-display)",
        color: colorStyle,
        fontSize: "12px",
      }}
    >
      <Icon aria-hidden="true" className="size-3 shrink-0" />
      {formatGrowthRate(growthRate)}
    </span>
  );
}
