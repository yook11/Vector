import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { Suspense } from "react";
import { getProtectedNavItems } from "@/components/layout/nav-items";
import { SlimMasthead } from "@/components/layout/SlimMasthead";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { PaperSurface, PaperTexture } from "@/components/paper";
import { UserMenu } from "@/features/auth";
import {
  BriefingDocument,
  getBriefingDetailViewModel,
} from "@/features/briefing";
import { ApiError } from "@/lib/api/error";
import { getCurrentSession, requireSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";

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

function BackLink() {
  return (
    <Link
      href="/briefing"
      className="inline-flex items-center gap-1.5 text-[12.5px] tracking-[0.04em] text-[var(--vector-ink-muted)] transition-colors hover:text-[var(--vector-ink)]"
      style={{ fontFamily: "var(--font-vector-maru)" }}
    >
      ← 一覧に戻る
    </Link>
  );
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
      <div className="pt-7 pb-4">
        <div className="mb-8">
          <BackLink />
        </div>
        <div className="mx-auto max-w-[640px] py-16 text-center">
          <h1
            className="text-[clamp(22px,3vw,30px)] font-bold text-[var(--vector-ink)]"
            style={{ fontFamily: "var(--font-vector-serif)" }}
          >
            {vm.category.name}
          </h1>
          <p
            className="mt-4 text-[14px] leading-[1.9] text-[var(--vector-ink-muted)]"
            style={{ fontFamily: "var(--font-vector-maru)" }}
          >
            まだ生成されていません。JST 月曜 00:05
            の自動生成、もしくは手動実行を待ってから再度ご確認ください。
          </p>
        </div>
      </div>
    );
  }

  return <BriefingDocument briefing={vm} />;
}

function BriefingDetailSkeleton() {
  const pulse =
    "animate-pulse rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]";
  return (
    <div className="pt-7 pb-4" aria-hidden="true">
      <div className={`mb-8 h-3 w-24 ${pulse}`} />
      <div className="mx-auto mb-12 flex max-w-[820px] flex-col items-center gap-4">
        <div className={`h-3 w-40 ${pulse}`} />
        <div className={`h-12 w-3/4 ${pulse}`} />
        <div className={`h-3 w-56 ${pulse}`} />
      </div>
      <div className="mx-auto mb-14 flex max-w-[34em] flex-col items-center gap-2">
        <div className={`h-4 w-full ${pulse}`} />
        <div className={`h-4 w-5/6 ${pulse}`} />
        <div className={`h-4 w-3/4 ${pulse}`} />
      </div>
      <div className="mx-auto flex max-w-[760px] flex-col gap-8">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex flex-col gap-3 pl-10">
            <div className={`h-5 w-1/2 ${pulse}`} />
            <div className={`h-16 w-full ${pulse}`} />
          </div>
        ))}
      </div>
    </div>
  );
}

export default async function BriefingDetailPage({
  params,
}: BriefingDetailPageProps) {
  const { category } = await params;
  // マストヘッドの nav 出し分けに session を使う。データ取得 gate は
  // BriefingDetailContent 側 (Suspense 単位) でも別途行う。
  const session = await requireSession();
  const isAdmin = narrowRole(session.user.role) === "admin";
  const navItems = getProtectedNavItems(isAdmin);

  return (
    <PaperSurface>
      <SlimMasthead
        navItems={navItems}
        activeHref="/briefing"
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
          <Suspense fallback={<BriefingDetailSkeleton />}>
            <BriefingDetailContent slug={category} />
          </Suspense>
        </main>
      </div>
    </PaperSurface>
  );
}
