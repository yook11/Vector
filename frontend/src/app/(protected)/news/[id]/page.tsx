import { notFound } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { NewsDetail } from "@/components/news/NewsDetail";
import { RelatedArticles } from "@/components/news/RelatedArticles";
import { ApiError, getNewsById, getSimilarNews } from "@/lib/api-client";
import type { NewsResponse } from "@/types";
import type { Metadata } from "next";

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
      title: `${article.analysis?.titleJa ?? article.titleOriginal} | Vector`,
    };
  } catch {
    return { title: "Article Not Found | Vector" };
  }
}

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;

  let article: NewsResponse;
  try {
    article = await getNewsById(Number(id));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  // Fetch similar articles — non-fatal (empty array if not yet embedded or on error)
  let similarArticles: NewsResponse[] = [];
  try {
    similarArticles = await getSimilarNews(Number(id), 5);
  } catch {
    // Graceful degradation: related articles are a progressive enhancement
  }

  return (
    <main className="mx-auto max-w-3xl p-6 space-y-4">
      <Button variant="ghost" size="sm" asChild>
        <Link href="/">&larr; Back to Dashboard</Link>
      </Button>
      <NewsDetail article={article} />
      <RelatedArticles articles={similarArticles} />
    </main>
  );
}
