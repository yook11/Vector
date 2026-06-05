import { WatchlistButton } from "@/features/watchlist";
import type { ArticleBrief } from "@/types/types.gen";
import { PaperArticleCard } from "./PaperArticleCard";

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
    <section className="mt-12 border-t border-[color-mix(in_oklab,var(--vector-ink)_14%,transparent)] pt-11">
      <div className="mb-6 flex items-center gap-3.5">
        <h2
          className="text-[22px] font-extrabold text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          関連記事
        </h2>
        <span className="h-px flex-1 bg-[color-mix(in_oklab,var(--vector-ink)_18%,transparent)]" />
      </div>
      <div className="grid grid-cols-1 gap-x-12 gap-y-8 md:grid-cols-2">
        {articles.map((article) => (
          <PaperArticleCard
            key={article.id}
            article={article}
            actionSlot={
              <WatchlistButton
                articleId={article.id}
                isWatched={watchedIds.has(article.id)}
                className="text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
              />
            }
          />
        ))}
      </div>
    </section>
  );
}
