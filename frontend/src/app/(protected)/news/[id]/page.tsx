import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { getProtectedNavItems } from "@/components/layout/nav-items";
import { SlimMasthead } from "@/components/layout/SlimMasthead";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { Skeleton } from "@/components/ui/skeleton";
import { UserMenu } from "@/features/auth";
import {
  getArticleById,
  getSimilarArticles,
  NewsDetail,
  PaperSurface,
  PaperTexture,
  RelatedArticles,
} from "@/features/news";
import { getWatchlistIds } from "@/features/watchlist";
import { ApiError } from "@/lib/api/error";
import { getCurrentSession, requireSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";
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
  // 404 判定の後・データ取得の前に置く。未認証はここで redirect。session は
  // マストヘッドの nav 出し分けに使う。
  const session = await requireSession();
  const isAdmin = narrowRole(session.user.role) === "admin";
  const navItems = getProtectedNavItems(isAdmin);

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
    <PaperSurface>
      <SlimMasthead
        navItems={navItems}
        activeHref="/"
        themeSlot={<ThemeToggle />}
        userMenuSlot={
          <UserMenu
            compact
            buttonClassName="rounded-none text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
            emailClassName="text-[var(--vector-ink-muted)]"
          />
        }
      />
      <div className="relative">
        <PaperTexture />
        <main className="relative z-10 mx-auto max-w-[1180px] px-5 pb-20 sm:px-8 lg:px-10">
          <NewsDetail
            article={article}
            isWatched={watchedIds.has(article.id)}
          />
          <Suspense fallback={<RelatedArticlesSkeleton />}>
            <RelatedArticlesAsync
              articlesPromise={similarPromise}
              watchedIds={watchedIds}
            />
          </Suspense>
        </main>
      </div>
    </PaperSurface>
  );
}
