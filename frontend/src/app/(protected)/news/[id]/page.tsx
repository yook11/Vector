import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { NewsDetail } from "@/components/news/NewsDetail";
import { RelatedArticles } from "@/components/news/RelatedArticles";
import { Button } from "@/components/ui/button";
import { ApiError, getNewsById, getSimilarNews } from "@/lib/api-client";
import type { NewsBrief, NewsDetail as NewsDetailData } from "@/types";

interface NewsPageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({
  params,
}: NewsPageProps): Promise<Metadata> {
  const { id } = await params;
  try {
    const article = await getNewsById(Number(id));
    return {
      title: `${article.translatedTitle} | Vector`,
    };
  } catch {
    return { title: "Article Not Found | Vector" };
  }
}

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;

  let article: NewsDetailData;
  try {
    article = await getNewsById(Number(id));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  // Fetch similar articles — non-fatal (empty array if not yet embedded or on error)
  let similarArticles: NewsBrief[] = [];
  try {
    similarArticles = await getSimilarNews(Number(id), 5);
  } catch {
    // Graceful degradation: related articles are a progressive enhancement
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
