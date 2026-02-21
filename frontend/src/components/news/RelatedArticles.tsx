import type { NewsResponse } from "@/types";
import { NewsCard } from "./NewsCard";

interface RelatedArticlesProps {
  articles: NewsResponse[];
}

export function RelatedArticles({ articles }: RelatedArticlesProps) {
  if (articles.length === 0) return null;

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">関連記事</h2>
      <div className="space-y-3">
        {articles.map((article) => (
          <NewsCard key={article.id} article={article} />
        ))}
      </div>
    </section>
  );
}
