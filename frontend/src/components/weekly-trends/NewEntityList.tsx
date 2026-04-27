import type { WeeklyNewEntity } from "@/types";

interface NewEntityListProps {
  entities: WeeklyNewEntity[];
}

export function NewEntityList({ entities }: NewEntityListProps) {
  if (entities.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        新規登場のエンティティはありません
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
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
            ×{entity.currentCount}
          </span>
        </li>
      ))}
    </ul>
  );
}
