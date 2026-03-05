"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
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
import {
  ApiError,
  clientDeleteSource,
  clientToggleSource,
} from "@/lib/client-api";
import type { NewsSourceResponse } from "@/types";
import { SourceFormDialog } from "./SourceFormDialog";

interface SourceTableProps {
  sources: NewsSourceResponse[];
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleString("ja-JP", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function SourceTable({ sources }: SourceTableProps) {
  const router = useRouter();
  const [toggling, setToggling] = useState<number | null>(null);

  async function handleToggle(id: number) {
    setToggling(id);
    try {
      const updated = await clientToggleSource(id);
      toast.success(
        `${updated.name} ${updated.isActive ? "enabled" : "disabled"}`,
      );
      router.refresh();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.detail : "Failed to toggle source",
      );
    } finally {
      setToggling(null);
    }
  }

  async function handleDelete(id: number, name: string) {
    try {
      await clientDeleteSource(id);
      toast.success(`Deleted "${name}"`);
      router.refresh();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.detail : "Failed to delete source",
      );
    }
  }

  if (sources.length === 0) {
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
          <TableHead>URL / Endpoint</TableHead>
          <TableHead className="text-center">Status</TableHead>
          <TableHead>Last Fetched</TableHead>
          <TableHead className="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sources.map((source) => (
          <TableRow key={source.id}>
            <TableCell className="font-medium">
              <div className="flex items-center gap-2">
                {source.name}
                {source.consecutiveErrors >= 5 && (
                  <Badge variant="destructive" title={source.lastErrorMessage ?? "Multiple consecutive errors"}>
                    Error
                  </Badge>
                )}
              </div>
            </TableCell>
            <TableCell>
              <Badge variant="outline">{source.sourceType.toUpperCase()}</Badge>
            </TableCell>
            <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">
              <span title={source.feedUrl ?? source.apiEndpoint ?? ""}>
                {source.feedUrl ?? source.apiEndpoint ?? "-"}
              </span>
            </TableCell>
            <TableCell className="text-center">
              <Switch
                checked={source.isActive}
                disabled={toggling === source.id}
                onCheckedChange={() => handleToggle(source.id)}
              />
            </TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {formatDate(source.lastFetchedAt)}
            </TableCell>
            <TableCell className="text-right">
              <div className="flex items-center justify-end gap-1">
                <SourceFormDialog
                  source={source}
                  trigger={
                    <Button variant="ghost" size="sm">
                      Edit
                    </Button>
                  }
                />
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button variant="ghost" size="sm" className="text-destructive">
                      Delete
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Delete &quot;{source.name}&quot;?</AlertDialogTitle>
                      <AlertDialogDescription>
                        This will permanently delete this source. Articles already
                        fetched from this source will remain.
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
