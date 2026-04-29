import { WatchlistButton } from "@/features/watchlist";
import type { ArticleBrief } from "@/types";
import { NewsCard } from "./NewsCard";

interface RelatedArticlesProps {
  articles: ArticleBrief[];
  /** Pattern B: 親 page から渡される watched ID 集合。 */
  watchedIds: Set<number>;
}

export function RelatedArticles({
  articles,
  watchedIds,
}: RelatedArticlesProps) {
  if (articles.length === 0) return null;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">関連記事</h2>
      <div className="space-y-3">
        {articles.map((article) => (
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
    </section>
  );
}
