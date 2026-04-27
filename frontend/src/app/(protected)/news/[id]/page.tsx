import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { NewsDetail } from "@/components/news/NewsDetail";
import { RelatedArticles } from "@/components/news/RelatedArticles";
import { Button } from "@/components/ui/button";
import { ApiError, getArticleById, getSimilarArticles } from "@/lib/api-client";
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

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;

  let article: ArticleDetailData;
  try {
    article = await getArticleById(Number(id));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  // Related articles are a progressive enhancement: failure must not break
  // the page, but we still log so embed/index regressions stay visible.
  let similarArticles: ArticleBrief[] = [];
  try {
    similarArticles = await getSimilarArticles(Number(id), 5);
  } catch (err) {
    console.error("Failed to load similar articles", err);
  }

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col gap-8">
        <Button variant="ghost" size="sm" asChild className="text-xs">
          <Link href="/">&larr; Back to Dashboard</Link>
        </Button>
        <NewsDetail article={article} />
        <RelatedArticles articles={similarArticles} />
      </div>
    </main>
  );
}
