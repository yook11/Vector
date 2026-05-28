"use client";

import Link from "next/link";
import { Separator } from "@/components/ui/separator";
import { useBuildSearchParamsHref } from "@/lib/search-params/client";
import { cn } from "@/lib/utils/cn";
import type { CategoryDetail } from "@/types/types.gen";

interface CategorySidebarProps {
  categories: CategoryDetail[];
  activeCategory?: string;
  /**
   * 各カテゴリ Link クリック時のフック。MobileSidebar から渡されて Sheet を
   * 閉じるなどに使われる。デスクトップ表示では渡さない。
   */
  onNavigate?: () => void;
}

export function CategorySidebar({
  categories,
  activeCategory,
  onNavigate,
}: CategorySidebarProps) {
  const buildHrefBase = useBuildSearchParamsHref();

  const isAll = !activeCategory;

  // Preserve existing filter params (sortOrder, perPage etc.) but reset
  // category and page when navigating between category facets.
  function buildHref(category: string | undefined): string {
    return buildHrefBase({ category, page: undefined });
  }

  const linkClass =
    "flex items-center justify-between px-3 py-2.5 text-sm rounded-xl transition-colors text-muted-foreground hover:text-foreground hover:bg-accent";

  return (
    <div className="flex flex-col gap-1.5 p-6">
      <h3 className="px-3 text-sm font-semibold text-foreground mb-1">
        Categories
      </h3>

      {/* All */}
      <Link
        href={buildHref(undefined)}
        {...(onNavigate && { onClick: onNavigate })}
        {...(isAll && { "aria-current": "page" as const })}
        className={cn(
          linkClass,
          isAll && "text-foreground font-medium bg-accent",
        )}
      >
        All
      </Link>

      <div className="px-3 flex justify-end">
        <span className="w-12 text-center text-xs font-medium text-muted-foreground whitespace-nowrap">
          Last 24h
        </span>
      </div>

      <Separator className="my-2" />

      {categories.map((cat) => {
        const isActiveCat = activeCategory === cat.slug;
        return (
          <Link
            key={cat.slug}
            href={buildHref(cat.slug)}
            {...(onNavigate && { onClick: onNavigate })}
            {...(isActiveCat && { "aria-current": "page" as const })}
            className={cn(
              linkClass,
              isActiveCat && "text-foreground font-medium bg-accent",
            )}
          >
            <span className="min-w-0 flex-1 line-clamp-2 leading-tight font-semibold">
              {cat.name}
            </span>
            {(cat.recentCount ?? 0) > 0 && (
              <span className="ml-2 w-12 text-center text-xs font-medium tabular-nums text-muted-foreground shrink-0">
                +{cat.recentCount}
              </span>
            )}
          </Link>
        );
      })}
    </div>
  );
}
