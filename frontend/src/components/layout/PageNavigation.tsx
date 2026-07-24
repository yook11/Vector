"use client";

import { Loader2Icon } from "lucide-react";
import Link, { useLinkStatus } from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import {
  type ComponentProps,
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils/cn";

type PendingNavigation = {
  sourceKey: string;
  originHref: string;
  targetHref: string;
  label: string;
};

type PageNavigationSnapshot = {
  pendingNavigation: PendingNavigation | null;
};

type PageNavigationActions = {
  reportPending: (sourceKey: string, href: string, label: string) => void;
  reportSettled: (sourceKey: string) => void;
  reset: () => void;
  setMobileStatusVisible: (visible: boolean) => void;
};

const PageNavigationSnapshotContext = createContext<PageNavigationSnapshot>({
  pendingNavigation: null,
});

const PageNavigationActionsContext = createContext<PageNavigationActions>({
  reportPending: () => undefined,
  reportSettled: () => undefined,
  reset: () => undefined,
  setMobileStatusVisible: () => undefined,
});

const PageNavigationProviderContext = createContext(false);

function normalizedHref(href: string): string {
  const url = new URL(href, "http://vector.local");
  return `${url.pathname}${url.search}`;
}

function navigationLabel(href: string): string {
  const pathname = new URL(href, "http://vector.local").pathname;

  if (pathname === "/") return "ニュースを読み込み中…";
  if (pathname.startsWith("/news/")) return "記事を読み込み中…";
  if (pathname.startsWith("/research")) return "Researchを読み込み中…";
  if (pathname === "/briefing" || pathname.startsWith("/briefing/")) {
    return "Briefingを読み込み中…";
  }
  if (pathname === "/trends") return "トレンドを読み込み中…";
  if (pathname === "/watchlist") return "ウォッチリストを読み込み中…";
  if (pathname === "/settings") return "Settingsを読み込み中…";
  if (pathname === "/admin/pipeline-status") {
    return "Pipeline Statusを読み込み中…";
  }
  if (pathname === "/admin/source-health") {
    return "Source Healthを読み込み中…";
  }
  return "画面を読み込み中…";
}

function isInternalNavigation(
  href: string,
  target: string | undefined,
  download: string | boolean | undefined,
): boolean {
  const url = new URL(href, "http://vector.local");
  return (
    url.origin === "http://vector.local" &&
    target === undefined &&
    download === undefined
  );
}

export function PageNavigationProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const committedHref = `${pathname ?? "/"}${searchParams.toString() ? `?${searchParams.toString()}` : ""}`;
  const committedHrefRef = useRef(committedHref);
  const [pendingNavigation, setPendingNavigation] =
    useState<PendingNavigation | null>(null);
  const [mobileStatusVisible, setMobileStatusVisible] = useState(false);

  committedHrefRef.current = committedHref;

  const reportPending = useCallback(
    (sourceKey: string, href: string, label: string) => {
      setPendingNavigation({
        sourceKey,
        originHref: committedHrefRef.current,
        targetHref: normalizedHref(href),
        label,
      });
    },
    [],
  );

  const reportSettled = useCallback((sourceKey: string) => {
    setPendingNavigation((current) =>
      current?.sourceKey === sourceKey ? null : current,
    );
  }, []);

  const reset = useCallback(() => setPendingNavigation(null), []);

  useEffect(() => {
    if (
      pendingNavigation !== null &&
      committedHref !== pendingNavigation.originHref
    ) {
      reset();
    }
  }, [committedHref, pendingNavigation, reset]);

  const snapshot = useMemo(() => ({ pendingNavigation }), [pendingNavigation]);
  const actions = useMemo(
    () => ({ reportPending, reportSettled, reset, setMobileStatusVisible }),
    [reportPending, reportSettled, reset],
  );

  return (
    <PageNavigationProviderContext.Provider value>
      <PageNavigationActionsContext.Provider value={actions}>
        <PageNavigationSnapshotContext.Provider value={snapshot}>
          {children}
          {pendingNavigation !== null && !mobileStatusVisible ? (
            <PageNavigationStatus label={pendingNavigation.label} />
          ) : null}
        </PageNavigationSnapshotContext.Provider>
      </PageNavigationActionsContext.Provider>
    </PageNavigationProviderContext.Provider>
  );
}

export function usePageNavigation() {
  const { pendingNavigation } = useContext(PageNavigationSnapshotContext);
  const { reset, setMobileStatusVisible } = useContext(
    PageNavigationActionsContext,
  );
  return { pendingNavigation, reset, setMobileStatusVisible };
}

type PendingAwareLinkProps = Omit<ComponentProps<typeof Link>, "href"> & {
  href: string;
};

/** Link の標準遷移を保ったまま、その lifecycle を共通 feedback へ通知する。 */
export function PendingAwareLink({
  children,
  download,
  href,
  target,
  ...props
}: PendingAwareLinkProps) {
  const hasPageNavigationProvider = useContext(PageNavigationProviderContext);
  const eligible = isInternalNavigation(href, target, download);

  return (
    <Link {...props} download={download} href={href} target={target}>
      {hasPageNavigationProvider ? (
        <LinkPendingObserver eligible={eligible} href={href} />
      ) : null}
      {children}
    </Link>
  );
}

function LinkPendingObserver({
  eligible,
  href,
}: {
  eligible: boolean;
  href: string;
}) {
  const sourceKey = useId();
  const { pending } = useLinkStatus();
  const { reportPending, reportSettled } = useContext(
    PageNavigationActionsContext,
  );
  const hadPending = useRef(false);

  useEffect(() => {
    if (!eligible) return;

    if (pending) {
      hadPending.current = true;
      reportPending(sourceKey, href, navigationLabel(href));
      return;
    }

    if (hadPending.current) {
      hadPending.current = false;
      reportSettled(sourceKey);
    }
  }, [eligible, href, pending, reportPending, reportSettled, sourceKey]);

  return null;
}

export function PageNavigationContent({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const { pendingNavigation } = useContext(PageNavigationSnapshotContext);
  const isPending = pendingNavigation !== null;

  return (
    <div
      aria-busy={isPending || undefined}
      className={cn("relative", className)}
    >
      {children}
      {isPending ? (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 z-40 bg-[color-mix(in_oklab,var(--vector-paper,#fff)_42%,transparent)] motion-reduce:transition-none"
          data-testid="page-navigation-overlay"
        />
      ) : null}
    </div>
  );
}

export function PageNavigationReset() {
  const { reset } = useContext(PageNavigationActionsContext);
  useEffect(() => reset(), [reset]);
  return null;
}

function PageNavigationStatus({ label }: { label: string }) {
  return (
    <div className="pointer-events-none fixed top-[max(env(safe-area-inset-top),0.5rem)] left-1/2 z-[60] -translate-x-1/2">
      <p
        aria-atomic="true"
        aria-label={label}
        aria-live="polite"
        className="flex items-center gap-2 rounded-full border border-[var(--vector-line,#d6d3d1)] bg-[var(--vector-surface,#fff)] px-3 py-2 text-sm font-medium text-[var(--vector-ink,#1c1917)] shadow-lg"
        role="status"
      >
        <Loader2Icon
          aria-hidden="true"
          className="size-4 animate-spin motion-reduce:animate-none"
        />
        {label}
      </p>
    </div>
  );
}
