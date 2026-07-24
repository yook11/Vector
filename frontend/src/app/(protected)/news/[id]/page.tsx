import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getArticleById,
  getSimilarArticles,
  NewsDetail,
  RelatedArticles,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
import { getCurrentSession, requireSession } from "@/lib/auth/guards";
import { PositiveIdParamSchema } from "@/lib/validation/id";
import type {
  ArticleBrief,
  ArticleDetail as ArticleDetailData,
} from "@/types/types.gen";

interface NewsPageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({
  params,
}: NewsPageProps): Promise<Metadata> {
  const { id } = await params;
  // 未認証は cached fetch (記事タイトル) を踏ませず generic title で返し、title
  // 経由の漏洩も塞ぐ。generateMetadata 内で redirect() は安定しないため、
  // getCurrentSession で判定して early-return する (本体 section の requireSession
  // と React.cache で DB hit を共有)。
  const session = await getCurrentSession();
  if (!session) {
    return { title: "Vector" };
  }
  // 本体側で notFound() に合流するため、metadata 側では title だけ返して終わる。
  // generateMetadata 内の notFound() は metadata 解決を未確定にしうるので避ける。
  const parsed = PositiveIdParamSchema.safeParse(id);
  if (!parsed.success) {
    return { title: "Article Not Found | Vector" };
  }
  try {
    // `getArticleById` は `'use cache'` を持つ。Page 本体でも同 id を await
    // するが、Next.js 16 の cache hit で実 backend hit は 1 回に収束する
    // (https://nextjs.org/docs/app/api-reference/functions/generate-metadata)。
    const article = await getArticleById(parsed.data);
    if (article === null) {
      return { title: "Article Not Found | Vector" };
    }
    return {
      title: `${article.translatedTitle} | Vector`,
    };
  } catch {
    // 5xx / network failure は Page の error.tsx が受け持つため、metadata は
    // generic に留めて not-found と誤認させない。
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
  // 独立した Suspense 単位なので本 section にも gate が要る。必ず try の外に
  // 置く (try 内だと redirect の NEXT_REDIRECT を下の catch が握り潰して
  // silent fail になる)。
  await requireSession();
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

function NewsDetailSkeleton() {
  const bar =
    "animate-pulse motion-reduce:animate-none rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]";

  return (
    <main className="relative z-10 mx-auto max-w-[1180px] px-5 pb-20 sm:px-8 lg:px-10">
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="mb-7 text-sm font-medium text-[var(--vector-ink-soft)]"
      >
        記事を読み込み中…
      </p>
      <div aria-hidden="true">
        <div className={`mb-7 h-4 w-32 ${bar}`} />
        <div className={`mb-4 h-11 w-4/5 ${bar}`} />
        <div className={`mb-6 h-5 w-2/3 ${bar}`} />
        <div className={`mb-9 h-14 w-full ${bar}`} />
        <div className="max-w-[860px] space-y-4">
          <div className={`h-5 w-full ${bar}`} />
          <div className={`h-5 w-full ${bar}`} />
          <div className={`h-5 w-5/6 ${bar}`} />
          <div className={`h-5 w-full ${bar}`} />
          <div className={`h-5 w-3/4 ${bar}`} />
        </div>
        <section className="mt-14 space-y-4">
          <div className={`h-7 w-28 ${bar}`} />
          <div className="grid grid-cols-1 gap-x-10 gap-y-8 md:grid-cols-2">
            {[0, 1].map((item) => (
              <div key={item} className={`h-28 ${bar}`} />
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}

function RelatedArticlesSkeleton() {
  return (
    <section className="mt-14 space-y-4" aria-hidden="true">
      <Skeleton className="h-7 w-28" />
      <div className="grid grid-cols-1 gap-x-10 gap-y-8 md:grid-cols-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
    </section>
  );
}

async function NewsDetailContent({
  articlePromise,
  similarPromise,
  watchedIdsPromise,
}: {
  articlePromise: Promise<ArticleDetailData | null>;
  similarPromise: Promise<ArticleBrief[]>;
  watchedIdsPromise: Promise<Set<number>>;
}) {
  const article = await articlePromise;
  if (article === null) {
    notFound();
  }
  const watchedIds = await watchedIdsPromise;

  return (
    <main className="relative z-10 mx-auto max-w-[1180px] px-5 pb-20 sm:px-8 lg:px-10">
      <NewsDetail article={article} isWatched={watchedIds.has(article.id)} />
      <Suspense fallback={<RelatedArticlesSkeleton />}>
        <RelatedArticlesAsync
          articlesPromise={similarPromise}
          watchedIds={watchedIds}
        />
      </Suspense>
    </main>
  );
}

export default async function NewsPage({ params }: NewsPageProps) {
  const { id } = await params;
  const parsed = PositiveIdParamSchema.safeParse(id);
  if (!parsed.success) {
    // URL malformed (`/news/abc`, `/news/-1`, `/news/1.5`, `/news/0`) は
    // backend に届ける前に 404 で塞ぐ。defense-in-depth と無駄な backend
    // hit 削減を兼ねる。
    notFound();
  }
  const articleId = parsed.data;

  // DAL gate: malformed URL を未認証でも 404 で返す現状の防御順序を保つため、
  // 404 判定の後・データ取得の前に置く。未認証はここで redirect。
  await requireSession();

  // Fire all fetches in parallel. similar は Suspense'd child に forward。
  // article 単独で 404 判定したいので await は分割する (Promise.all だと
  // watchlist 側の失敗を 404 と誤認しうる)。
  const articlePromise = getArticleById(articleId);
  const similarPromise = getSimilarArticles(articleId, 5);
  const watchedIdsPromise = getWatchlistIds();

  return (
    <PaperSurface>
      <ShellMasthead />
      <div className="relative">
        <PaperTexture />
        <PageNavigationContent>
          <Suspense fallback={<NewsDetailSkeleton />}>
            <NewsDetailContent
              articlePromise={articlePromise}
              similarPromise={similarPromise}
              watchedIdsPromise={watchedIdsPromise}
            />
          </Suspense>
        </PageNavigationContent>
      </div>
    </PaperSurface>
  );
}
