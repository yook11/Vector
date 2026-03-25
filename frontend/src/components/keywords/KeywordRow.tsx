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

interface KeywordRowProps {
  keyword: KeywordResponse;
}

export function KeywordRow({ keyword }: KeywordRowProps) {
  const router = useRouter();
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    setDeleting(true);
    try {
      await deleteKeyword(keyword.id);
      toast.success(`Deleted "${keyword.name}"`);
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
        <KeywordTag name={keyword.name} category={keyword.category} />
      </TableCell>
      <TableCell>
        <Badge variant="secondary" className="text-xs">
          {keyword.category.name}
        </Badge>
      </TableCell>
      <TableCell className="text-center">{keyword.articleCount}</TableCell>
      <TableCell className="text-center">
        <Badge
          variant={keyword.status === "official" ? "default" : "outline"}
          className="text-xs"
        >
          {keyword.status}
        </Badge>
      </TableCell>
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
                Are you sure you want to delete &ldquo;{keyword.name}&rdquo;?
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
