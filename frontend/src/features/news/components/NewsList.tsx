import { WatchlistButton } from "@/features/watchlist";
import type { ArticleBrief } from "@/types";
import { NewsCard } from "./NewsCard";

interface NewsListProps {
  items: ArticleBrief[];
  /**
   * 認証済 user の watched article ID 集合 (Pattern B)。
   * 未ログインや未取得時は空 Set を渡す。
   */
  watchedIds: Set<number>;
}

export function NewsList({ items, watchedIds }: NewsListProps) {
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
        <p className="text-sm font-medium">No articles found</p>
        <p className="text-xs mt-1">
          Try adjusting your filters or fetch new articles.
        </p>
      </div>
    );
  }

  return (
    <div className="grid gap-x-8 gap-y-0 md:grid-cols-2 xl:grid-cols-3 grid-apple-dividers [&>*]:py-6 [&>*]:border-b [&>*]:border-border">
      {items.map((article) => (
        <NewsCard
          key={article.id}
          article={article}
          actionSlot={
            <WatchlistButton
              articleId={article.id}
              isWatched={watchedIds.has(article.id)}
            />
          }
        />
      ))}
    </div>
  );
}
