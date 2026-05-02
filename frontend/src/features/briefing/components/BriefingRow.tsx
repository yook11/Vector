import Link from "next/link";
import { formatDate } from "@/lib/date";
import type { BriefingCategory, BriefingListLatest } from "@/types";

interface BriefingRowProps {
  category: BriefingCategory;
  latest: BriefingListLatest;
  isCurrentWeek: boolean;
}

export function BriefingRow({
  category,
  latest,
  isCurrentWeek,
}: BriefingRowProps) {
  return (
    <li>
      <Link
        href={`/briefing/${category.slug}`}
        className="group flex items-baseline justify-between gap-4 py-4 px-2 -mx-2 rounded-md transition-colors hover:bg-muted/40"
      >
        <div className="flex flex-col gap-1 min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="text-xs font-medium uppercase tracking-wider text-foreground/60">
              {category.name}
            </span>
            {!isCurrentWeek && (
              <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {formatDate(latest.weekStart)}
              </span>
            )}
          </div>
          <p className="text-sm text-foreground line-clamp-2 group-hover:text-foreground/90">
            {latest.headlineExcerpt}
          </p>
        </div>
        <span
          aria-hidden="true"
          className="text-foreground/40 group-hover:text-foreground/80 transition-colors shrink-0"
        >
          →
        </span>
      </Link>
    </li>
  );
}
