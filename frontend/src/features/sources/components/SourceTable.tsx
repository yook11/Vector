"use client";

import { useOptimistic, useTransition } from "react";
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
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { isRedirectError } from "@/lib/utils/redirect-error";
import { toastError } from "@/lib/utils/toast-error";
import type { NewsSourceDetail } from "@/types";
import { activateSource } from "../api/activate-source";
import { deactivateSource } from "../api/deactivate-source";
import { deleteSource } from "../api/delete-source";

interface SourceTableProps {
  sources: NewsSourceDetail[];
}

type OptimisticAction =
  | { type: "toggle"; id: number; isActive: boolean }
  | { type: "delete"; id: number };

function reducer(
  state: NewsSourceDetail[],
  action: OptimisticAction,
): NewsSourceDetail[] {
  switch (action.type) {
    case "toggle":
      return state.map((s) =>
        s.id === action.id ? { ...s, isActive: action.isActive } : s,
      );
    case "delete":
      return state.filter((s) => s.id !== action.id);
  }
}

export function SourceTable({ sources }: SourceTableProps) {
  const [optimisticSources, applyOptimistic] = useOptimistic(sources, reducer);
  const [pending, startTransition] = useTransition();

  function handleToggle(source: NewsSourceDetail) {
    startTransition(async () => {
      const next = !source.isActive;
      applyOptimistic({ type: "toggle", id: source.id, isActive: next });
      try {
        const updated = next
          ? await activateSource(source.id)
          : await deactivateSource(source.id);
        toast.success(
          `${updated.name} ${updated.isActive ? "enabled" : "disabled"}`,
        );
      } catch (err) {
        // 未認証は requireSessionForAction が redirect throw する経路。digest
        // 判定で再 throw して Next.js navigation を起動させる (握り潰すと
        // login 画面に遷移できず toast だけが出る silent fail になる)。
        if (isRedirectError(err)) throw err;
        toastError(err, "ソースの更新に失敗しました");
      }
    });
  }

  function handleDelete(id: number, name: string) {
    startTransition(async () => {
      applyOptimistic({ type: "delete", id });
      try {
        await deleteSource(id);
        toast.success(`Deleted "${name}"`);
      } catch (err) {
        if (isRedirectError(err)) throw err;
        toastError(err, "ソースの削除に失敗しました");
      }
    });
  }

  if (optimisticSources.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <p className="text-lg font-medium">No sources configured</p>
        <p className="text-sm">Add a news source to start fetching articles.</p>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Type</TableHead>
          <TableHead>Endpoint URL</TableHead>
          <TableHead className="text-center">Status</TableHead>
          <TableHead className="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {optimisticSources.map((source) => (
          <TableRow key={source.id}>
            <TableCell className="font-medium">{source.name}</TableCell>
            <TableCell>
              <Badge variant="outline">{source.sourceType.toUpperCase()}</Badge>
            </TableCell>
            <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">
              <span title={source.endpointUrl}>{source.endpointUrl}</span>
            </TableCell>
            <TableCell className="text-center">
              <Switch
                checked={source.isActive}
                disabled={pending}
                onCheckedChange={() => handleToggle(source)}
                aria-label={`「${source.name}」の有効化を切り替える`}
              />
            </TableCell>
            <TableCell className="text-right">
              <div className="flex items-center justify-end gap-1">
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive"
                      disabled={pending}
                    >
                      Delete
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>
                        Delete “{source.name}”?
                      </AlertDialogTitle>
                      <AlertDialogDescription>
                        This will permanently delete this source. Articles
                        already fetched from this source will remain.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction
                        onClick={() => handleDelete(source.id, source.name)}
                      >
                        Delete
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
