import type { WeeklyEntityTrend } from "@/types";

interface HotEntityListProps {
  entities: WeeklyEntityTrend[];
}

export function HotEntityList({ entities }: HotEntityListProps) {
  if (entities.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        該当するエンティティはありません
      </p>
    );
  }

  return (
    <ul className="flex flex-col divide-y divide-border">
      {entities.map((entity) => (
        <li
          key={`${entity.type}:${entity.name}`}
          className="flex items-baseline justify-between gap-3 py-2.5"
        >
          <div className="flex flex-col gap-0.5 min-w-0">
            <span className="text-sm font-medium text-foreground truncate">
              {entity.name}
            </span>
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {entity.type}
            </span>
          </div>
          <div className="shrink-0 flex items-baseline gap-2 text-xs text-muted-foreground tabular-nums">
            <span>
              {entity.previousCount} → {entity.currentCount}
            </span>
            <span className="font-medium text-foreground">
              ×{entity.hotnessScore.toFixed(1)}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
