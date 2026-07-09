"use client";

import { Trash2 } from "lucide-react";
import { useTransition } from "react";
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
import { Button } from "@/components/ui/button";
import { isRedirectError } from "@/lib/utils/redirect-error";
import { toastError } from "@/lib/utils/toast-error";
import { deleteResearchThread } from "../api/delete-research-thread";

interface DeleteThreadButtonProps {
  threadId: string;
  title: string;
}

export function DeleteThreadButton({
  threadId,
  title,
}: DeleteThreadButtonProps) {
  const [pending, startTransition] = useTransition();

  function handleDelete() {
    startTransition(async () => {
      try {
        await deleteResearchThread(threadId);
      } catch (err) {
        if (isRedirectError(err)) throw err;
        toastError(err, "スレッドを削除できませんでした");
      }
    });
  }

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="text-[var(--vector-ink-muted)] hover:text-destructive"
          aria-label="スレッドを削除"
          title="スレッドを削除"
          disabled={pending}
        >
          <Trash2 aria-hidden="true" />
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>スレッドを削除しますか</AlertDialogTitle>
          <AlertDialogDescription>
            「{title}」を削除します。この操作は取り消せません。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>キャンセル</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={pending}
            onClick={handleDelete}
          >
            削除
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
