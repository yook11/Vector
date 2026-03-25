"use client";

import Link from "next/link";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { clientGetGroupArticles } from "@/lib/client-api";
import type { NewsResponse } from "@/types";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function DuplicateBadge({
  duplicateCount,
  articleGroupId,
}: {
  duplicateCount: number;
  articleGroupId: number;
}) {
  const [articles, setArticles] = useState<NewsResponse[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleOpen(open: boolean) {
    if (!open || articles !== null) return;
    setLoading(true);
    try {
      const data = await clientGetGroupArticles(articleGroupId);
      setArticles(data);
    } catch {
      // Failed to fetch group articles
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog onOpenChange={handleOpen}>
      <DialogTrigger asChild>
        <Badge
          variant="secondary"
          className="cursor-pointer text-xs hover:bg-secondary/80"
        >
          +{duplicateCount} sources
        </Badge>
      </DialogTrigger>
      <DialogContent className="max-w-lg max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Related Sources ({duplicateCount + 1})</DialogTitle>
        </DialogHeader>
        {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
        {articles && (
          <ul className="space-y-3">
            {articles.map((a) => (
              <li key={a.id} className="border-b pb-3 last:border-b-0">
                <Link
                  href={`/news/${a.id}`}
                  className="text-sm font-medium hover:underline"
                >
                  {a.analysis?.translatedTitle ?? a.originalTitle}
                </Link>
                <p className="text-xs text-muted-foreground mt-1">
                  {a.sourceName} &middot; {formatDate(a.publishedAt)}
                </p>
                {a.analysis?.summary && (
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                    {a.analysis.summary}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
