import { EmptyState } from "@/components/feedback/EmptyState";
import { WatchlistButton } from "@/features/watchlist";
import type { AnalyzedArticlePreview } from "@/types/types.gen";
import { ArticleCard } from "./ArticleCard";

interface ArticleListProps {
  items: AnalyzedArticlePreview[];
  watchedIds: Set<number>;
}

export function ArticleList({ items, watchedIds }: ArticleListProps) {
  if (items.length === 0) {
    return (
      <div className="border-b border-[var(--vector-rule)] py-16">
        <EmptyState
          title="記事がありません"
          description="カテゴリや並び順を変えて、もう一度確認してください。"
        />
      </div>
    );
  }

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
              className="size-7 rounded-none text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
              iconClassName="size-4"
            />
          }
        />
      ))}
    </div>
  );
}
