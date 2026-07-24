"use client";

import { Loader2, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, useTransition } from "react";
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
import { useResearchNavigation } from "./ResearchNavigationBoundary";
import { useResearchOperation } from "./ResearchOperationBoundary";

interface DeleteThreadButtonProps {
  threadId: string;
  title: string;
}

export function DeleteThreadButton({
  threadId,
  title,
}: DeleteThreadButtonProps) {
  const { isNavigationPending } = useResearchNavigation();
  const { claimOperation, operation, releaseOperation } =
    useResearchOperation();
  const [isOpen, setIsOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [, startTransition] = useTransition();
  const deletionLockRef = useRef(false);
  const disabled = isDeleting || isNavigationPending || operation !== null;

  function handleDelete() {
    if (disabled || deletionLockRef.current) return;
    if (!claimOperation("delete")) return;
    deletionLockRef.current = true;
    setIsDeleting(true);
    startTransition(async () => {
      try {
        await deleteResearchThread(threadId);
      } catch (err) {
        if (isRedirectError(err)) throw err;
        deletionLockRef.current = false;
        setIsDeleting(false);
        releaseOperation("delete");
        toastError(err, "スレッドを削除できませんでした");
      }
    });
  }

  useEffect(
    () => () => {
      releaseOperation("delete");
    },
    [releaseOperation],
  );

  return (
    <AlertDialog
      open={isOpen}
      onOpenChange={(nextOpen) => {
        if (!disabled) setIsOpen(nextOpen);
      }}
    >
      <AlertDialogTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="text-[var(--vector-ink-muted)] hover:text-destructive"
          aria-label="スレッドを削除"
          title="スレッドを削除"
          disabled={disabled}
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
          <AlertDialogCancel disabled={disabled}>キャンセル</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={disabled}
            aria-busy={isDeleting}
            onClick={(event) => {
              event.preventDefault();
              handleDelete();
            }}
          >
            {isDeleting && (
              <Loader2
                aria-hidden="true"
                className="animate-spin motion-reduce:animate-none"
              />
            )}
            {isDeleting ? "削除中…" : "削除"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
