import { WatchlistButton } from "@/features/watchlist";
import type { AnalyzedArticlePreview } from "@/types/types.gen";
import { ArticleCard } from "./ArticleCard";

interface ArticleListProps {
  items: AnalyzedArticlePreview[];
  watchedIds: Set<number>;
  onWatchedChange?: (articleId: number, isWatched: boolean) => void;
}

export function ArticleList({
  items,
  watchedIds,
  onWatchedChange,
}: ArticleListProps) {
  return (
    <div className="grid grid-cols-1 gap-x-12 gap-y-[30px] md:grid-cols-2">
      {items.map((article) => (
        <ArticleCard
          key={article.id}
          article={article}
          actionSlot={
            <WatchlistButton
              articleId={article.id}
              isWatched={watchedIds.has(article.id)}
              onWatchedChange={(isWatched) =>
                onWatchedChange?.(article.id, isWatched)
              }
              className="size-7 rounded-none text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
              iconClassName="size-4"
            />
          }
        />
      ))}
    </div>
  );
}
