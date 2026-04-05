"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { CategoryDetailResponse } from "@/types";

interface CategorySidebarProps {
  categories: CategoryDetailResponse[];
  activeCategory?: string;
  activeKeyword?: string;
}

export function CategorySidebar({
  categories,
  activeCategory,
  activeKeyword,
}: CategorySidebarProps) {
  const searchParams = useSearchParams();
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    if (activeCategory) initial.add(activeCategory);
    return initial;
  });

  const toggleExpand = (slug: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  };

  const isAll = !activeCategory && !activeKeyword;

  // Preserve existing filter params (sentiment, sortBy, etc.) when navigating
  function buildHref(overrides: Record<string, string | undefined>): string {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    // Remove category/keyword params first
    params.delete("category");
    params.delete("keyword");
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

      {/* Category drilldown */}
      {categories.map((cat) => {
        const isActiveCat =
          activeCategory === cat.slug && activeKeyword === undefined;
        const isExpanded = expanded.has(cat.slug);

        return (
          <div key={cat.slug}>
            <div className="flex items-center">
              <button
                type="button"
                onClick={() => toggleExpand(cat.slug)}
                className="flex items-center justify-center size-8 shrink-0 text-muted-foreground hover:text-foreground transition-colors"
                aria-label={isExpanded ? "Collapse" : "Expand"}
              >
                <ChevronRight
                  className={cn(
                    "size-3.5 transition-transform",
                    isExpanded && "rotate-90",
                  )}
                />
              </button>
              <Link
                href={buildHref({ category: cat.slug })}
                className={cn(
                  "flex-1 flex items-center justify-between pr-3 py-2.5 text-sm rounded-xl transition-colors text-muted-foreground hover:text-foreground hover:bg-neutral-100 dark:hover:bg-neutral-800/40",
                  isActiveCat &&
                    "text-foreground font-medium bg-neutral-100 dark:bg-neutral-800/50",
                )}
              >
                <span className="truncate">{cat.name}</span>
                <span className="ml-2 text-xs tabular-nums text-neutral-400 dark:text-neutral-600">
                  {cat.articleCount}
                </span>
              </Link>
            </div>

            {isExpanded && cat.keywords.length > 0 && (
              <div className="ml-8 flex flex-col gap-0.5">
                {cat.keywords.map((kw) => {
                  const isActiveKw = activeKeyword === kw.name;
                  return (
                    <Link
                      key={kw.name}
                      href={buildHref({
                        category: cat.slug,
                        keyword: kw.name,
                      })}
                      className={cn(
                        linkClass,
                        isActiveKw &&
                          "text-foreground font-medium bg-neutral-100 dark:bg-neutral-800/50",
                      )}
                    >
                      <span className="truncate">{kw.name}</span>
                      <span className="ml-2 text-xs tabular-nums text-neutral-400 dark:text-neutral-600">
                        {kw.articleCount}
                      </span>
                    </Link>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
