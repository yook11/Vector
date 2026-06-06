import type { BriefingArticleSummary, BriefingKeyArticle } from "@/types";
import { ArticleCard } from "./ArticleCard";

interface KeyArticleBlockProps {
  keyArticle: BriefingKeyArticle;
  articlesById: Map<number, BriefingArticleSummary>;
}

export function KeyArticleBlock({
  keyArticle,
  articlesById,
}: KeyArticleBlockProps) {
  const article = articlesById.get(keyArticle.articleId);

  return (
    <section className="flex flex-col gap-3">
      {article && <ArticleCard article={article} />}
      <p className="text-sm leading-relaxed text-foreground/90 whitespace-pre-line">
        {keyArticle.significance}
      </p>
    </section>
  );
}
