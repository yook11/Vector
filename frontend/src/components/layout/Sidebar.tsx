import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { KeywordResponse } from "@/types";

interface SidebarProps {
  keywords: KeywordResponse[];
  activeKeywordId?: number;
}

export function Sidebar({ keywords, activeKeywordId }: SidebarProps) {
  return (
    <div className="flex flex-col gap-1 py-4">
      <h3 className="px-4 text-sm font-semibold text-muted-foreground mb-2">
        Keywords
      </h3>
      <Link
        href="/"
        className={cn(
          "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
          activeKeywordId === undefined && "bg-accent font-medium",
        )}
      >
        All
      </Link>
      {keywords.map((kw) => (
        <Link
          key={kw.id}
          href={`/?keywordId=${kw.id}`}
          className={cn(
            "flex items-center justify-between px-4 py-2 text-sm rounded-md transition-colors hover:bg-accent",
            activeKeywordId === kw.id && "bg-accent font-medium",
          )}
        >
          <span className="truncate">{kw.name}</span>
          <Badge variant="secondary" className="ml-2 text-xs">
            {kw.articleCount}
          </Badge>
        </Link>
      ))}
    </div>
  );
}
