"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
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
import { KeywordTag } from "./KeywordTag";
import { SubscriptionToggle } from "./SubscriptionToggle";
import { deleteKeyword, updateKeyword } from "@/lib/api-client";
import type { KeywordResponse } from "@/types";

interface KeywordRowProps {
  keyword: KeywordResponse;
  subscribedKeywordIds?: Set<number>;
}

export function KeywordRow({ keyword, subscribedKeywordIds }: KeywordRowProps) {
  const router = useRouter();
  const [toggling, setToggling] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleToggle(checked: boolean) {
    setToggling(true);
    try {
      await updateKeyword(keyword.id, { isActive: checked });
      router.refresh();
    } catch {
      toast.error("Failed to update keyword");
    } finally {
      setToggling(false);
    }
  }

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
        <KeywordTag keyword={keyword.keyword} category={keyword.category} />
      </TableCell>
      <TableCell className="text-muted-foreground">
        {keyword.category}
      </TableCell>
      <TableCell className="text-center">{keyword.articleCount}</TableCell>
      <TableCell className="text-center">
        <Switch
          checked={keyword.isActive}
          onCheckedChange={handleToggle}
          disabled={toggling}
        />
      </TableCell>
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
