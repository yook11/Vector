import type { BriefingArticleSummary, BriefingStory } from "@/types";
import { ArticleCard } from "./ArticleCard";

interface StoryBlockProps {
  story: BriefingStory;
  articlesById: Map<number, BriefingArticleSummary>;
}

export function StoryBlock({ story, articlesById }: StoryBlockProps) {
  const articles = story.articleIds
    .map((id) => articlesById.get(id))
    .filter((a): a is BriefingArticleSummary => a !== undefined);

  return (
    <section className="flex flex-col gap-3">
      <p className="text-sm font-medium leading-relaxed text-foreground whitespace-pre-line">
        {story.takeaway}
      </p>
      {articles.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {articles.map((article) => (
            <ArticleCard key={article.id} article={article} />
          ))}
        </div>
      )}
    </section>
  );
}
