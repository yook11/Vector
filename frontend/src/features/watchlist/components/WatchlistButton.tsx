"use client";

import { Bookmark } from "lucide-react";
import { useOptimistic, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils/cn";
import { toastError } from "@/lib/utils/toast-error";
import { addToWatchlist } from "../api/add-to-watchlist";
import { removeFromWatchlist } from "../api/remove-from-watchlist";

interface WatchlistButtonProps {
  articleId: number;
  isWatched: boolean;
}

export function WatchlistButton({
  articleId,
  isWatched,
}: WatchlistButtonProps) {
  const [optimisticIsWatched, setOptimisticIsWatched] =
    useOptimistic(isWatched);
  const [pending, startTransition] = useTransition();

  function handleToggle() {
    startTransition(async () => {
      const next = !optimisticIsWatched;
      setOptimisticIsWatched(next);
      try {
        if (next) {
          await addToWatchlist(articleId);
        } else {
          await removeFromWatchlist(articleId);
        }
      } catch (err) {
        // throw 時は React が optimistic state を base に自動 revert する。
        // 401 (未認証) は requireSessionForAction が redirect する経路に乗る
        // ので、ここに来るのは backend エラーや 5xx 等の操作失敗のみ。
        console.error("Watchlist toggle failed", err);
        toastError(
          err,
          next
            ? "ウォッチリストへの追加に失敗しました"
            : "ウォッチリストから削除できませんでした",
        );
      }
    });
  }

  const label = optimisticIsWatched
    ? "Remove from watchlist"
    : "Add to watchlist";
  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-8 w-8"
      onClick={handleToggle}
      disabled={pending}
      aria-label={label}
      aria-pressed={optimisticIsWatched}
      title={label}
    >
      <Bookmark
        aria-hidden="true"
        className={cn("size-4", optimisticIsWatched && "fill-current")}
      />
    </Button>
  );
}
