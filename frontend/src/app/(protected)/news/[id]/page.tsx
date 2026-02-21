import { notFound } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { NewsDetail } from "@/components/news/NewsDetail";
import { ApiError, getNewsById } from "@/lib/api-client";
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

  let article;
  try {
    article = await getNewsById(Number(id));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  return (
    <main className="mx-auto max-w-3xl p-6 space-y-4">
      <Button variant="ghost" size="sm" asChild>
        <Link href="/">&larr; Back to Dashboard</Link>
      </Button>
      <NewsDetail article={article} />
    </main>
  );
}
