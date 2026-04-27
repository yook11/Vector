"use client";

import { Bookmark } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  clientAddToWatchlist,
  clientRemoveFromWatchlist,
} from "@/lib/client-api";

interface WatchlistButtonProps {
  articleId: number;
  isWatched: boolean;
}

export function WatchlistButton({
  articleId,
  isWatched: initialIsWatched,
}: WatchlistButtonProps) {
  const [isWatched, setIsWatched] = useState(initialIsWatched);
  const [pending, setPending] = useState(false);
  const router = useRouter();

  async function handleToggle() {
    setPending(true);
    try {
      if (isWatched) {
        await clientRemoveFromWatchlist(articleId);
        setIsWatched(false);
      } else {
        await clientAddToWatchlist(articleId);
        setIsWatched(true);
      }
      router.refresh();
    } catch (err) {
      console.error("Watchlist toggle failed", err);
      toast.error(
        isWatched
          ? "Failed to remove from watchlist"
          : "Failed to add to watchlist",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-8 w-8"
      onClick={handleToggle}
      disabled={pending}
      title={isWatched ? "Remove from watchlist" : "Add to watchlist"}
    >
      <Bookmark className={`h-4 w-4 ${isWatched ? "fill-current" : ""}`} />
    </Button>
  );
}
