"use client";

import { Bookmark } from "lucide-react";
import { useOptimistic, useTransition } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
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
        console.error("Watchlist toggle failed", err);
        toast.error(
          next
            ? "Failed to add to watchlist"
            : "Failed to remove from watchlist",
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
        className={`h-4 w-4 ${optimisticIsWatched ? "fill-current" : ""}`}
      />
    </Button>
  );
}
