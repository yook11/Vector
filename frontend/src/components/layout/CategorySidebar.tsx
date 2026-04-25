"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { CategoryDetailResponse } from "@/types";

interface CategorySidebarProps {
  categories: CategoryDetailResponse[];
  activeCategory?: string;
}

export function CategorySidebar({
  categories,
  activeCategory,
}: CategorySidebarProps) {
  const searchParams = useSearchParams();

  const isAll = !activeCategory;

  // Preserve existing filter params (sentiment, sortBy, etc.) when navigating
  function buildHref(overrides: Record<string, string | undefined>): string {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.delete("category");
    params.delete("page");

    for (const [key, value] of Object.entries(overrides)) {
      if (value !== undefined) {
        params.set(key, value);
      }
    }

    const qs = params.toString();
    return qs ? `/?${qs}` : "/";
  }

  const linkClass =
    "flex items-center justify-between px-3 py-2.5 text-sm rounded-xl transition-colors text-muted-foreground hover:text-foreground hover:bg-neutral-100 dark:hover:bg-neutral-800/40";

  return (
    <div className="flex flex-col gap-1.5 p-6">
      <h3 className="px-3 text-sm font-semibold text-foreground mb-1">
        Categories
      </h3>

      {/* All */}
      <Link
        href={buildHref({})}
        className={cn(
          linkClass,
          isAll &&
            "text-foreground font-medium bg-neutral-100 dark:bg-neutral-800/50",
        )}
      >
        All
      </Link>

      <Separator className="my-2" />

      {categories.map((cat) => {
        const isActiveCat = activeCategory === cat.slug;
        return (
          <Link
            key={cat.slug}
            href={buildHref({ category: cat.slug })}
            className={cn(
              linkClass,
              isActiveCat &&
                "text-foreground font-medium bg-neutral-100 dark:bg-neutral-800/50",
            )}
          >
            <span className="truncate">{cat.name}</span>
            {cat.recentCount > 0 && (
              <span className="ml-2 text-xs tabular-nums text-neutral-400 dark:text-neutral-600">
                {cat.recentCount}
              </span>
            )}
          </Link>
        );
      })}
    </div>
  );
}
