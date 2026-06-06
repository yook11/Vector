import type { BriefingWatchPoint } from "@/types";

interface WatchPointsProps {
  watchPoints: BriefingWatchPoint[];
}

export function WatchPoints({ watchPoints }: WatchPointsProps) {
  return (
    <ul className="flex flex-col gap-3">
      {watchPoints.map((wp) => (
        <li
          key={wp.statement}
          className="text-sm leading-relaxed text-foreground/90 pl-4 border-l-2 border-border/60"
        >
          {wp.statement}
        </li>
      ))}
    </ul>
  );
}
