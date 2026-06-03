import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { Suspense } from "react";
import { EmptyState } from "@/components/feedback/EmptyState";
import { PageContainer } from "@/components/layout/PageContainer";
import { Skeleton } from "@/components/ui/skeleton";
import {
  BriefingDisclaimer,
  getBriefingDetailViewModel,
  StoryBlock,
} from "@/features/briefing";
import { ApiError } from "@/lib/api/error";
import { getCurrentSession, requireSession } from "@/lib/auth/guards";
import { formatDate } from "@/lib/date";
import type { BriefingArticleSummary } from "@/types";

interface BriefingDetailPageProps {
  params: Promise<{ category: string }>;
}

export async function generateMetadata({
  params,
}: BriefingDetailPageProps): Promise<Metadata> {
  const { category } = await params;
  // 未認証は cached fetch (カテゴリ名) を踏ませず generic title で返す。
  // generateMetadata 内で redirect() は安定しないため getCurrentSession で判定。
  const session = await getCurrentSession();
  if (!session) {
    return { title: "Briefing | Vector" };
  }
  try {
    const vm = await getBriefingDetailViewModel(category);
    return { title: `${vm.category.name} Briefing | Vector` };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return { title: "Briefing Not Found | Vector" };
    }
    return { title: "Briefing | Vector" };
  }
}

async function BriefingDetailContent({ slug }: { slug: string }) {
  // DAL gate: layout の認可は PPR の別 prerender 単位を守らないため、データ
  // 取得の前にここで認可する。
  await requireSession();
  await connection();
  let vm: Awaited<ReturnType<typeof getBriefingDetailViewModel>>;
  try {
    vm = await getBriefingDetailViewModel(slug);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    throw err;
  }

  if (vm.state === "empty") {
    return (
      <>
        <BackLink />
        <h1 className="text-base font-medium">{vm.category.name}</h1>
        <EmptyState
          title="まだ生成されていません"
          description="JST 月曜 00:05 の自動生成、もしくは手動 CLI 実行を待ってから再度ご確認ください"
        />
      </>
    );
  }

  const articlesById = new Map<number, BriefingArticleSummary>(
    vm.articles.map((a) => [a.id, a]),
  );

  return (
    <>
      <BackLink />
      <header className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          {vm.category.name} · {formatDate(vm.weekStart)} 週 ·{" "}
          {vm.inputArticleCount} 件の記事から生成
        </p>
        <h1 className="text-xl sm:text-2xl font-medium tracking-tight leading-snug">
          {vm.headline}
        </h1>
      </header>

      <article className="text-sm leading-loose text-foreground/90 whitespace-pre-line">
        {vm.overview}
      </article>

      {vm.stories.length > 0 && (
        <section className="flex flex-col gap-6 border-t border-border/60 pt-6">
          <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            参考にした記事
          </h2>
          <div className="flex flex-col gap-8">
            {vm.stories.map((story) => (
              <StoryBlock
                key={story.articleIds.join(",")}
                story={story}
                articlesById={articlesById}
              />
            ))}
          </div>
        </section>
      )}

      <BriefingDisclaimer />
    </>
  );
}

function BackLink() {
  return (
    <Link
      href="/briefing"
      className="text-xs text-muted-foreground hover:text-foreground transition-colors w-fit"
    >
      ← 一覧に戻る
    </Link>
  );
}

function BriefingDetailSkeleton() {
  return (
    <div className="flex flex-col gap-8" aria-hidden="true">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-7 w-3/4" />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </div>
      {[0, 1].map((i) => (
        <div key={i} className="flex flex-col gap-3">
          <Skeleton className="h-5 w-3/4" />
          <Skeleton className="h-16 w-full" />
        </div>
      ))}
    </div>
  );
}

export default async function BriefingDetailPage({
  params,
}: BriefingDetailPageProps) {
  const { category } = await params;
  return (
    <PageContainer maxWidth="3xl" gap={8}>
      <Suspense fallback={<BriefingDetailSkeleton />}>
        <BriefingDetailContent slug={category} />
      </Suspense>
    </PageContainer>
  );
}
