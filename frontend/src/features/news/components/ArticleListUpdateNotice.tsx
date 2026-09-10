"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";

const POLL_INTERVAL_MS = 60_000;
const REQUEST_TIMEOUT_MS = 10_000;

interface ArticleListUpdateNoticeProps {
  displayedRevision: string;
}

interface RevisionResponse {
  revision: string;
}

function isRevisionResponse(value: unknown): value is RevisionResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    "revision" in value &&
    typeof value.revision === "string"
  );
}

export function ArticleListUpdateNotice({
  displayedRevision,
}: ArticleListUpdateNoticeProps) {
  const router = useRouter();
  const previousDisplayedRevision = useRef(displayedRevision);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [refreshing, startRefresh] = useTransition();

  useEffect(() => {
    if (previousDisplayedRevision.current === displayedRevision) return;
    previousDisplayedRevision.current = displayedRevision;
    setUpdateAvailable(false);
  }, [displayedRevision]);

  useEffect(() => {
    if (updateAvailable || refreshing) return;

    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let requestController: AbortController | undefined;

    const schedule = (delay: number) => {
      clearTimeout(timer);
      timer = setTimeout(checkRevision, delay);
    };

    const checkRevision = async () => {
      if (!active || document.hidden || requestController !== undefined) return;

      requestController = new AbortController();
      const controller = requestController;
      let completedCurrentRequest = false;
      const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const response = await fetch("/api/news/revision", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("revision request failed");
        const body: unknown = await response.json();
        if (!isRevisionResponse(body))
          throw new Error("invalid revision response");
        if (!active || controller !== requestController) return;
        if (body.revision !== displayedRevision) {
          setUpdateAvailable(true);
          return;
        }
      } catch {
        // 確認失敗は表示中の一覧へ影響させず、次の確認機会に再試行する。
      } finally {
        clearTimeout(timeout);
        if (controller === requestController) {
          requestController = undefined;
          completedCurrentRequest = true;
        }
      }
      if (active && completedCurrentRequest && !document.hidden) {
        schedule(POLL_INTERVAL_MS);
      }
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearTimeout(timer);
        requestController?.abort();
        requestController = undefined;
        return;
      }
      schedule(0);
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    if (!document.hidden) schedule(0);

    return () => {
      active = false;
      clearTimeout(timer);
      requestController?.abort();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [displayedRevision, refreshing, updateAvailable]);

  if (!updateAvailable && !refreshing) return null;

  return (
    <aside
      aria-live="polite"
      className="relative z-10 mx-5 mb-5 flex flex-wrap items-center justify-center gap-x-4 gap-y-2 border-y border-[var(--vector-rule)] bg-[color-mix(in_oklab,var(--vector-paper)_88%,var(--vector-accent)_12%)] px-4 py-3 text-[13px] text-[var(--vector-ink)] sm:mx-8 lg:mx-10"
    >
      <span>新しい記事が追加されました</span>
      <button
        type="button"
        disabled={refreshing}
        onClick={() => {
          if (refreshing) return;
          startRefresh(() => {
            setUpdateAvailable(false);
            router.refresh();
          });
        }}
        className="border-b border-[var(--vector-accent)] font-semibold text-[var(--vector-accent-ink)] transition-opacity hover:opacity-70 disabled:cursor-wait disabled:opacity-50"
      >
        {refreshing ? "更新中…" : "一覧を更新"}
      </button>
    </aside>
  );
}
