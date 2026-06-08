import type { BriefingWatchPoint } from "@/types";

interface WatchPointsProps {
  watchPoints: BriefingWatchPoint[];
}

/** 今後の注目点を番号付きの予測リスト (紙面様式) で描画する。 */
export function WatchPoints({ watchPoints }: WatchPointsProps) {
  return (
    <div className="mx-auto max-w-[860px]">
      {watchPoints.map((wp, i) => (
        <div
          key={wp.statement}
          className={
            i === 0
              ? "grid grid-cols-[minmax(52px,84px)_1fr] items-start gap-x-[clamp(20px,4vw,48px)] border-t-[3px] border-double border-[var(--vector-ink)] py-[clamp(22px,3vw,32px)]"
              : "grid grid-cols-[minmax(52px,84px)_1fr] items-start gap-x-[clamp(20px,4vw,48px)] border-t border-[var(--vector-line)] py-[clamp(22px,3vw,32px)]"
          }
        >
          <span
            className="text-[clamp(34px,4.4vw,52px)] italic leading-[0.9] text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {String(i + 1).padStart(2, "0")}
          </span>
          <p
            className="text-pretty text-[clamp(16px,1.7vw,19px)] leading-[1.85] text-[var(--vector-ink)]"
            style={{ fontFamily: "var(--font-vector-serif)" }}
          >
            {wp.statement}
          </p>
        </div>
      ))}
    </div>
  );
}
