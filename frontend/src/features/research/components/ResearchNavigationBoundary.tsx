"use client";

import { Loader2 } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  useTransition,
} from "react";

export type ResearchNavigationTarget =
  | {
      kind: "thread";
      href: string;
      threadId: string;
      label: string;
    }
  | {
      kind: "new";
      href: "/research";
      label: "新しいスレッド";
    }
  | {
      kind: "more";
      href: string;
      label: "さらに表示";
    };

interface ResearchNavigationContextValue {
  isNavigationPending: boolean;
  pendingTarget: ResearchNavigationTarget | null;
  navigate: (target: ResearchNavigationTarget) => void;
}

const ResearchNavigationContext =
  createContext<ResearchNavigationContextValue | null>(null);

function pendingStatus(target: ResearchNavigationTarget): string {
  switch (target.kind) {
    case "thread":
      return `「${target.label}」を読み込み中…`;
    case "new":
      return "新しいスレッドを準備中…";
    case "more":
      return "スレッド一覧を読み込み中…";
  }
}

export function useResearchNavigation(): ResearchNavigationContextValue {
  const value = useContext(ResearchNavigationContext);
  if (value === null) {
    throw new Error(
      "useResearchNavigation must be used within ResearchNavigationBoundary",
    );
  }
  return value;
}

interface ResearchNavigationBoundaryProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export function ResearchNavigationBoundary({
  sidebar,
  children,
}: ResearchNavigationBoundaryProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [, startTransition] = useTransition();
  const [pendingTarget, setPendingTarget] =
    useState<ResearchNavigationTarget | null>(null);
  const navigationLockRef = useRef(false);
  const disconnectedRef = useRef(false);
  const query = searchParams.toString();
  const currentHref = query ? `${pathname}?${query}` : pathname;
  const pendingPathname =
    pendingTarget === null
      ? null
      : new URL(pendingTarget.href, "http://research.local").pathname;
  const routeCommitted =
    pendingTarget !== null &&
    (pendingTarget.kind === "more"
      ? currentHref === pendingTarget.href
      : pathname === pendingPathname);

  // Next route cacheから復帰したinstanceに旧navigation lockを残さない。
  useEffect(() => {
    if (disconnectedRef.current) {
      navigationLockRef.current = false;
      setPendingTarget(null);
    }
    disconnectedRef.current = false;
    return () => {
      disconnectedRef.current = true;
    };
  }, []);

  useEffect(() => {
    if (!routeCommitted) return;
    navigationLockRef.current = false;
    setPendingTarget(null);
  }, [routeCommitted]);

  // cache済み旧subtreeのusePathnameは更新されないため、実URL commitも監視する。
  useEffect(() => {
    if (pendingTarget === null) return;
    const target = pendingTarget;
    const targetPathname = new URL(target.href, window.location.origin)
      .pathname;
    let animationFrame = 0;

    function clearAfterBrowserCommit() {
      const browserHref = `${window.location.pathname}${window.location.search}`;
      const committed =
        target.kind === "more"
          ? browserHref === target.href
          : window.location.pathname === targetPathname;
      if (committed) {
        navigationLockRef.current = false;
        setPendingTarget(null);
        return;
      }
      animationFrame = window.requestAnimationFrame(clearAfterBrowserCommit);
    }

    animationFrame = window.requestAnimationFrame(clearAfterBrowserCommit);
    return () => window.cancelAnimationFrame(animationFrame);
  }, [pendingTarget]);

  const navigate = useCallback(
    (target: ResearchNavigationTarget) => {
      if (navigationLockRef.current) return;
      navigationLockRef.current = true;
      setPendingTarget(target);
      startTransition(() => {
        router.push(target.href);
      });
    },
    [router],
  );

  const isNavigationPending = pendingTarget !== null;
  const status = pendingTarget === null ? "" : pendingStatus(pendingTarget);

  return (
    <ResearchNavigationContext.Provider
      value={{ isNavigationPending, pendingTarget, navigate }}
    >
      <main
        aria-busy={isNavigationPending}
        className="relative z-10 mx-auto flex h-[calc(100dvh-5.5rem)] w-full min-w-0 max-w-[1280px] flex-col overflow-hidden border-x border-b border-[var(--vector-rule)] bg-[var(--vector-surface)] md:flex-row"
      >
        {sidebar}
        <div className="relative flex min-h-0 min-w-0 flex-1">
          {children}
          {pendingTarget !== null && (
            <div
              data-testid="research-navigation-overlay"
              aria-hidden="true"
              className="pointer-events-none absolute inset-0 flex items-start justify-end bg-[var(--vector-surface-2)]/40 p-4 motion-safe:animate-in motion-safe:fade-in motion-safe:duration-150"
            >
              <div className="flex max-w-full items-center gap-2 rounded-md border border-[var(--vector-rule)] bg-[var(--vector-surface)]/95 px-4 py-3 text-sm font-medium text-[var(--vector-ink)] shadow-sm">
                <Loader2
                  aria-hidden="true"
                  className="shrink-0 animate-spin motion-reduce:animate-none"
                />
                <p className="min-w-0 truncate">{status}</p>
              </div>
            </div>
          )}
        </div>
        <div
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className="sr-only"
        >
          {status}
        </div>
      </main>
    </ResearchNavigationContext.Provider>
  );
}
