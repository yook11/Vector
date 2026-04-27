import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { NewsDetail } from "@/components/news/NewsDetail";
import { RelatedArticles } from "@/components/news/RelatedArticles";
import { Button } from "@/components/ui/button";
import { getArticleById } from "@/features/news/api/get-article-by-id";
import { getSimilarArticles } from "@/features/news/api/get-similar-articles";
import { ApiError } from "@/lib/api/error";
import type { ArticleBrief, ArticleDetail as ArticleDetailData } from "@/types";

interface NewsPageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({
  params,
}: NewsPageProps): Promise<Metadata> {
  const { id } = await params;
  try {
    const article = await getArticleById(Number(id));
    return {
      title: `${article.translatedTitle} | Vector`,
    };
  } catch {
    return { title: "Article Not Found | Vector" };
  }
}

async function RelatedArticlesAsync({
  articlesPromise,
}: {
  articlesPromise: Promise<ArticleBrief[]>;
}) {
  // Related articles are a progressive enhancement: failure must not break
  // the page, but we still log so embed/index regressions stay visible.
  let articles: ArticleBrief[] = [];
  try {
    articles = await articlesPromise;
  } catch (err) {
    console.error("Failed to load similar articles", err);
  }
  return <RelatedArticles articles={articles} />;
}

function RelatedArticlesSkeleton() {
  return (
    <section className="space-y-3" aria-hidden="true">
      <div className="h-6 w-24 rounded bg-muted/60 animate-pulse" />
      <div className="space-y-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-24 rounded-md bg-muted/40 animate-pulse" />
        ))}
      </div>
    </section>
  );
}

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;
  const articleId = Number(id);

  // Fire both fetches in parallel. The similar-articles promise is forwarded
  // to a Suspense'd child so it can stream in after the article renders.
  const articlePromise = getArticleById(articleId);
  const similarPromise = getSimilarArticles(articleId, 5);

  let article: ArticleDetailData;
  try {
    article = await articlePromise;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
        <Button variant="ghost" size="sm" asChild className="text-xs">
          <Link href="/">&larr; Back to Dashboard</Link>
        </Button>
        <NewsDetail article={article} />
        <Suspense fallback={<RelatedArticlesSkeleton />}>
          <RelatedArticlesAsync articlesPromise={similarPromise} />
        </Suspense>
      </div>
    </main>
  );
}
