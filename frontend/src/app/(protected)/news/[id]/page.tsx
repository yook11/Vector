import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getArticleById,
  getSimilarArticles,
  NewsDetail,
  RelatedArticles,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
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
    // `getArticleById` は `'use cache'` を持つ。Page 本体でも同 id を await
    // するが、Next.js 16 の cache hit で実 backend hit は 1 回に収束する
    // (https://nextjs.org/docs/app/api-reference/functions/generate-metadata)。
    const article = await getArticleById(Number(id));
    return {
      title: `${article.translatedTitle} | Vector`,
    };
  } catch (err) {
    // 404/410 は Page 本体の `notFound()` 経路に流すため metadata 側でも
    // 専用タイトル。それ以外 (5xx 含む) は error.tsx が UI を受け持つので
    // metadata は generic に留め、誤って 5xx を "Not Found" と誤認させない。
    if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
      return { title: "Article Not Found | Vector" };
    }
    return { title: "Vector" };
  }
}

async function RelatedArticlesAsync({
  articlesPromise,
  watchedIds,
}: {
  articlesPromise: Promise<ArticleBrief[]>;
  watchedIds: Set<number>;
}) {
  // Related articles are a progressive enhancement: failure must not break
  // the page, but we still log so embed/index regressions stay visible.
  let articles: ArticleBrief[] = [];
  try {
    articles = await articlesPromise;
  } catch (err) {
    console.error("Failed to load similar articles", err);
  }
  return <RelatedArticles articles={articles} watchedIds={watchedIds} />;
}

function RelatedArticlesSkeleton() {
  return (
    <section className="space-y-3" aria-hidden="true">
      <Skeleton className="h-6 w-24" />
      <div className="space-y-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
    </section>
  );
}

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;
  const articleId = Number(id);

  // Fire all fetches in parallel. similar は Suspense'd child に forward。
  // article 単独で 404 判定したいので await は分割する (Promise.all だと
  // watchlist 側の失敗を 404 と誤認しうる)。
  const articlePromise = getArticleById(articleId);
  const similarPromise = getSimilarArticles(articleId, 5);
  const watchedIdsPromise = getWatchlistIds();

  let article: ArticleDetailData;
  try {
    article = await articlePromise;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }
  const watchedIds = await watchedIdsPromise;

  return (
    <PageContainer maxWidth="3xl">
      <Button variant="ghost" size="sm" asChild className="text-xs">
        <Link href="/">&larr; Back to Dashboard</Link>
      </Button>
      <NewsDetail article={article} isWatched={watchedIds.has(article.id)} />
      <Suspense fallback={<RelatedArticlesSkeleton />}>
        <RelatedArticlesAsync
          articlesPromise={similarPromise}
          watchedIds={watchedIds}
        />
      </Suspense>
    </PageContainer>
  );
}
