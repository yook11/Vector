import Link from "next/link";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { KeywordResponse } from "@/types";

interface SidebarProps {
  keywords: KeywordResponse[];
  activeKeywordId?: number;
  subscribedKeywordIds?: number[];
  showMyKeywords?: boolean;
}

export function Sidebar({
  keywords,
  activeKeywordId,
  subscribedKeywordIds,
  showMyKeywords,
}: SidebarProps) {
  const subscribedSet = new Set(subscribedKeywordIds ?? []);
  const hasSubscriptions = subscribedSet.size > 0;

  return (
    <div className="flex flex-col gap-1 py-4">
      <h3 className="px-4 text-sm font-semibold text-muted-foreground mb-2">
        Keywords
      </h3>
      <Link
        href="/"
        className={cn(
          "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
          activeKeywordId === undefined && !showMyKeywords && "bg-accent font-medium",
        )}
      >
        All
      </Link>
      {hasSubscriptions && (
        <Link
          href="/?myKeywords=true"
          className={cn(
            "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
            showMyKeywords && "bg-accent font-medium",
          )}
        >
          My Keywords
        </Link>
      )}
      {keywords.map((kw) => (
        <Link
          key={kw.id}
          href={`/?keywordId=${kw.id}`}
          className={cn(
            "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
            activeKeywordId === kw.id && "bg-accent font-medium",
          )}
        >
          <span className="truncate">
            {subscribedSet.has(kw.id) ? `* ${kw.keyword}` : kw.keyword}
          </span>
          <Badge variant="secondary" className="ml-2 text-xs">
            {kw.articleCount}
          </Badge>
        </Link>
      ))}
    </div>
  );
}
