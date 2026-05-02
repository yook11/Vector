import type { BriefingCategory } from "@/types";

interface BriefingEmptyRowProps {
  category: BriefingCategory;
}

/**
 * 未生成カテゴリ行。clickable にせず、灰色で表示することで「存在は知って
 * いるがまだ briefing がない」を可視化する。404 ではなく「データが揃って
 * いない」を表現する空状態。
 */
export function BriefingEmptyRow({ category }: BriefingEmptyRowProps) {
  return (
    <li
      className="flex items-baseline justify-between gap-4 py-4 px-2 -mx-2 text-muted-foreground"
      aria-label={`${category.name}: まだ生成されていません`}
    >
      <div className="flex flex-col gap-1 min-w-0 flex-1">
        <span className="text-xs font-medium uppercase tracking-wider opacity-60">
          {category.name}
        </span>
        <p className="text-sm opacity-60">まだ生成されていません</p>
      </div>
    </li>
  );
}
