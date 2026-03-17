import type { NewsResponse } from "@/types";
import { NewsCard } from "./NewsCard";

export function NewsList({ items }: { items: NewsResponse[] }) {
  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <p className="text-lg font-medium">No articles found</p>
        <p className="text-sm">
          Try adjusting your filters or fetch new articles.
        </p>
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((article) => (
        <NewsCard key={article.id} article={article} />
      ))}
    </div>
  );
}
