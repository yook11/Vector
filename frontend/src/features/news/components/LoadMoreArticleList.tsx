"use client";

import { Loader2Icon } from "lucide-react";
import { useRouter } from "next/navigation";
import { type ReactNode, useState, useTransition } from "react";
import { cn } from "@/lib/utils/cn";
import { isRedirectError } from "@/lib/utils/redirect-error";
import type { AnalyzedArticlePreviewList } from "@/types/types.gen";
import { ArticleList } from "./ArticleList";

interface LoadMoreArticleListProps {
  initialList: AnalyzedArticlePreviewList;
  /** initialList のうちウォッチ済みの記事 ID。 */
  initialWatchedIds: Set<number>;
  /** 続きの記事と、そのうちウォッチ済みの記事 ID を返す。 */
  loadMore: (cursor: string) => Promise<{
    list: AnalyzedArticlePreviewList;
    watchedIds: Set<number>;
  }>;
  emptyState: ReactNode;
  /** ウォッチリスト用。読み込んだ後にウォッチを外した記事を出さない。 */
  showsOnlyWatched?: boolean;
}

/** 記事一覧を表示し、「さらに読み込む」で続きを下に足していく。 */
export function LoadMoreArticleList(props: LoadMoreArticleListProps) {
  const { bfcacheId } = useRouter();
  // 新しい遷移では読み込んだ続きを捨てて先頭から出し、戻る/進む・Server Action 後の
  // 再描画では保つ。この条件はデータから導けないため bfcacheId を key にする。
  return <LoadedArticles key={bfcacheId} {...props} />;
}

function LoadedArticles({
  initialList,
  initialWatchedIds,
  loadMore,
  emptyState,
  showsOnlyWatched = false,
}: LoadMoreArticleListProps) {
  const [items, setItems] = useState(initialList.items);
  const [watchedIds, setWatchedIds] = useState(initialWatchedIds);
  // 反映中に旧 backend の応答 (nextCursor 無し) を受けても「続きなし」として扱う。
  const [nextCursor, setNextCursor] = useState(initialList.nextCursor ?? null);
  const [hasLoadedMore, setHasLoadedMore] = useState(false);
  const [failed, setFailed] = useState(false);
  const [isLoading, startLoading] = useTransition();

  const visibleItems = showsOnlyWatched
    ? items.filter((article) => watchedIds.has(article.id))
    : items;

  // 一覧の状態はサーバーから最初に一度だけ受け取るため、ウォッチの操作結果はここで反映する。
  function handleWatchedChange(articleId: number, isWatched: boolean) {
    setWatchedIds((current) => {
      const next = new Set(current);
      if (isWatched) next.add(articleId);
      else next.delete(articleId);
      return next;
    });
  }

  function handleLoadMore() {
    if (nextCursor === null) return;
    const cursor = nextCursor;
    startLoading(async () => {
      setFailed(false);
      try {
        const next = await loadMore(cursor);
        setItems((current) => [...current, ...next.list.items]);
        setWatchedIds((current) => new Set([...current, ...next.watchedIds]));
        setNextCursor(next.list.nextCursor ?? null);
        setHasLoadedMore(true);
      } catch (err) {
        // 未ログインの redirect は握り潰さず Next.js の遷移に渡す。
        if (isRedirectError(err)) throw err;
        console.error("Loading more articles failed", err);
        setFailed(true);
      }
    });
  }

  if (visibleItems.length === 0 && nextCursor === null) return emptyState;

  return (
    <>
      <ArticleList
        items={visibleItems}
        watchedIds={watchedIds}
        onWatchedChange={handleWatchedChange}
      />
      <div
        aria-live="polite"
        className="flex flex-col items-center gap-2 pt-8 pb-2 text-[12px] tracking-[0.12em] text-[var(--vector-ink-muted)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {failed && <p>読み込めませんでした</p>}
        {nextCursor !== null ? (
          <button
            type="button"
            disabled={isLoading}
            aria-busy={isLoading}
            onClick={handleLoadMore}
            className="inline-flex items-center gap-1.5 border-b border-[var(--vector-line)] pb-1 disabled:cursor-not-allowed disabled:opacity-35"
          >
            {isLoading
              ? "読み込み中…"
              : failed
                ? "もう一度読み込む"
                : "さらに読み込む"}
            <Loader2Icon
              aria-hidden="true"
              className={cn(
                "size-3 shrink-0 animate-spin motion-reduce:animate-none transition-opacity duration-200",
                isLoading ? "opacity-100" : "opacity-0",
              )}
            />
          </button>
        ) : (
          hasLoadedMore && <p>これ以上の記事はありません</p>
        )}
      </div>
    </>
  );
}
