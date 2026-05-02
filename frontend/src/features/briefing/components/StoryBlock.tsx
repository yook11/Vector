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
    <section className="flex flex-col gap-4">
      <h2 className="text-base font-medium tracking-tight">{story.title}</h2>
      <p className="text-sm leading-relaxed text-foreground/80 whitespace-pre-line">
        {story.analysis}
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
