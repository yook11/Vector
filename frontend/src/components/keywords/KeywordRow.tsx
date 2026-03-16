"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import { clientDeleteKeyword as deleteKeyword } from "@/lib/client-api";
import type { KeywordResponse } from "@/types";
import { KeywordTag } from "./KeywordTag";
import { SubscriptionToggle } from "./SubscriptionToggle";

interface KeywordRowProps {
  keyword: KeywordResponse;
  subscribedKeywordIds?: Set<number>;
}

export function KeywordRow({ keyword, subscribedKeywordIds }: KeywordRowProps) {
  const router = useRouter();
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    setDeleting(true);
    try {
      await deleteKeyword(keyword.id);
      toast.success(`Deleted "${keyword.keyword}"`);
      router.refresh();
    } catch {
      toast.error("Failed to delete keyword");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <TableRow>
      <TableCell>
        <KeywordTag keyword={keyword.keyword} categories={keyword.categories} />
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {keyword.categories && keyword.categories.length > 0 ? (
            keyword.categories.map((cat) => (
              <Badge key={cat.slug} variant="secondary" className="text-xs">
                {cat.name}
              </Badge>
            ))
          ) : (
            <span className="text-muted-foreground text-sm">—</span>
          )}
        </div>
      </TableCell>
      <TableCell className="text-center">{keyword.articleCount}</TableCell>
      {subscribedKeywordIds && (
        <TableCell className="text-center">
          <SubscriptionToggle
            keywordId={keyword.id}
            isSubscribed={subscribedKeywordIds.has(keyword.id)}
          />
        </TableCell>
      )}
      <TableCell className="text-right">
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button variant="destructive" size="sm" disabled={deleting}>
              Delete
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete keyword?</AlertDialogTitle>
              <AlertDialogDescription>
                Are you sure you want to delete &ldquo;{keyword.keyword}&rdquo;?
                This action cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={handleDelete}>
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </TableCell>
    </TableRow>
  );
}
