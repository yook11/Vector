"use client";

import { Bookmark } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils/cn";

export function MockWatchlistButton() {
  const [watched, setWatched] = useState(false);
  const label = watched ? "Remove from watchlist" : "Add to watchlist";
  return (
    <Button
      variant="ghost"
      size="icon"
      className="h-8 w-8"
      onClick={() => setWatched((v) => !v)}
      aria-label={label}
      aria-pressed={watched}
      title={label}
    >
      <Bookmark
        aria-hidden="true"
        className={cn("size-4", watched && "fill-current")}
      />
    </Button>
  );
}
