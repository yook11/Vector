import Link from "next/link";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/date";
import type { ArticleBrief } from "@/types/types.gen";

export function NewsCard({
  article,
  actionSlot,
}: {
  article: ArticleBrief;
  actionSlot?: ReactNode;
}) {
  return (
    <article className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-2">
        <Badge
          variant="secondary"
          className="min-w-0 shrink truncate"
          title={article.category.name}
        >
          {article.category.name}
        </Badge>
        {actionSlot && <div className="-mt-1 -mr-1 shrink-0">{actionSlot}</div>}
      </div>

      <Link href={`/news/${article.id}`} className="group mt-3 block">
        <h3 className="text-lg font-medium text-foreground line-clamp-3 group-hover:text-primary transition-colors">
          {article.translatedTitle}
        </h3>
      </Link>

      <p className="mt-2 text-sm leading-relaxed text-muted-foreground line-clamp-2">
        {article.summary}
      </p>

      <div className="mt-auto flex items-center gap-1.5 pt-5 text-xs text-muted-foreground">
        <span className="font-medium text-foreground/80">
          {article.source.name}
        </span>
        <span className="text-muted-foreground/50">·</span>
        <span>{formatDate(article.publishedAt)}</span>
      </div>
    </article>
  );
}
