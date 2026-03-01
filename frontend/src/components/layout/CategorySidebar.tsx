"use client";

import { useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { KeywordCategoryDetailResponse } from "@/types";

interface CategorySidebarProps {
  categories: KeywordCategoryDetailResponse[];
  activeKwCategoryId?: number;
  activeKeywordId?: number;
  subscribedKeywordIds?: number[];
  showMyKeywords?: boolean;
}

export function CategorySidebar({
  categories,
  activeKwCategoryId,
  activeKeywordId,
  subscribedKeywordIds,
  showMyKeywords,
}: CategorySidebarProps) {
  const searchParams = useSearchParams();
  const [expanded, setExpanded] = useState<Set<number>>(() => {
    const initial = new Set<number>();
    if (activeKwCategoryId) initial.add(activeKwCategoryId);
    return initial;
  });

  const toggleExpand = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const hasSubscriptions = (subscribedKeywordIds ?? []).length > 0;
  const isAll = !activeKwCategoryId && !activeKeywordId && !showMyKeywords;

  // Preserve existing filter params (sentiment, sortBy, etc.) when navigating
  function buildHref(overrides: Record<string, string | undefined>): string {
    const params = new URLSearchParams(searchParams.toString());
    // Remove category/keyword params first
    params.delete("kwCategoryId");
    params.delete("keywordId");
    params.delete("myKeywords");
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
    "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent";

  return (
    <div className="flex flex-col gap-1 py-4">
      <h3 className="px-4 text-sm font-semibold text-muted-foreground mb-2">
        Categories
      </h3>

      {/* All */}
      <Link
        href={buildHref({})}
        className={cn(linkClass, isAll && "bg-accent font-medium")}
      >
        All
      </Link>

      {/* My Keywords */}
      {hasSubscriptions && (
        <Link
          href={buildHref({ myKeywords: "true" })}
          className={cn(linkClass, showMyKeywords && "bg-accent font-medium")}
        >
          My Keywords
        </Link>
      )}

      {/* Category drilldown */}
      {categories.map((cat) => {
        const isActiveCat =
          activeKwCategoryId === cat.id && activeKeywordId === undefined;
        const isExpanded = expanded.has(cat.id);

        return (
          <div key={cat.id}>
            <div className="flex items-center">
              <button
                type="button"
                onClick={() => toggleExpand(cat.id)}
                className="flex items-center justify-center w-8 h-8 shrink-0 text-muted-foreground hover:text-foreground transition-colors"
                aria-label={isExpanded ? "Collapse" : "Expand"}
              >
                <ChevronRight
                  className={cn(
                    "h-4 w-4 transition-transform",
                    isExpanded && "rotate-90",
                  )}
                />
              </button>
              <Link
                href={buildHref({ kwCategoryId: String(cat.id) })}
                className={cn(
                  "flex-1 flex items-center justify-between pr-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
                  isActiveCat && "bg-accent font-medium",
                )}
              >
                <span className="truncate">{cat.name}</span>
                <Badge variant="secondary" className="ml-2 text-xs">
                  {cat.articleCount}
                </Badge>
              </Link>
            </div>

            {isExpanded && cat.keywords.length > 0 && (
              <div className="ml-8">
                {cat.keywords.map((kw) => {
                  const isActiveKw = activeKeywordId === kw.id;
                  return (
                    <Link
                      key={kw.id}
                      // kwCategoryId is included for sidebar active-state rendering
                      // only; the API filters by keywordId alone (see news.py elif chain).
                      href={buildHref({
                        kwCategoryId: String(cat.id),
                        keywordId: String(kw.id),
                      })}
                      className={cn(
                        linkClass,
                        isActiveKw && "bg-accent font-medium",
                      )}
                    >
                      <span className="truncate">{kw.keyword}</span>
                      <Badge variant="secondary" className="ml-2 text-xs">
                        {kw.articleCount}
                      </Badge>
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
