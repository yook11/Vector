import { Suspense } from "react";
import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";
import { ResearchRouteHost } from "@/features/research-client";
import { requireSession } from "@/lib/auth/guards";

function ResearchWorkspaceSkeleton() {
  const bar =
    "animate-pulse motion-reduce:animate-none rounded-sm bg-[color-mix(in_oklab,var(--vector-ink)_10%,transparent)]";

  return (
    <div
      className="relative z-10 flex min-h-0 w-full flex-1"
      role="status"
      aria-label="Researchを読み込み中…"
      aria-live="polite"
      aria-atomic="true"
    >
      <p className="absolute left-5 top-5 z-10 text-sm font-medium text-[var(--vector-ink-soft)] sm:left-7 sm:top-7">
        Researchを読み込み中…
      </p>
      <div
        aria-hidden="true"
        className="min-h-0 w-full motion-reduce:animate-none"
      >
        <div className="grid min-h-0 w-full grid-cols-1 lg:grid-cols-[15rem_minmax(0,1fr)]">
          <aside className="hidden border-r border-[var(--vector-rule)] p-4 lg:block">
            <div className={`mb-5 h-8 w-24 ${bar}`} />
            <div className="space-y-3">
              {[0, 1, 2, 3, 4].map((item) => (
                <div key={item} className={`h-12 w-full ${bar}`} />
              ))}
            </div>
          </aside>
          <section className="flex min-w-0 flex-col p-5 sm:p-7">
            <div className={`h-5 w-40 ${bar}`} />
            <div className={`mt-7 h-24 w-full ${bar}`} />
            <div className={`mt-5 h-16 w-4/5 ${bar}`} />
            <div className={`mt-auto h-24 w-full ${bar}`} />
          </section>
        </div>
      </div>
    </div>
  );
}

export default async function ResearchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireSession();

  return (
    <PaperSurface className="flex h-dvh min-h-0 flex-col overflow-hidden [&>header]:shrink-0">
      <ShellMasthead />
      <div className="relative flex min-h-0 w-full flex-1 overflow-hidden">
        <PaperTexture />
        <PageNavigationContent className="flex min-h-0 w-full flex-1">
          <ResearchRouteHost initialFallback={<ResearchWorkspaceSkeleton />}>
            <Suspense fallback={null}>{children}</Suspense>
          </ResearchRouteHost>
        </PageNavigationContent>
      </div>
    </PaperSurface>
  );
}
