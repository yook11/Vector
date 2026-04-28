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
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError } from "@/lib/api/error";
import type { NewsSourceDetail } from "@/types";
import { activateSource } from "../api/activate-source";
import { deactivateSource } from "../api/deactivate-source";
import { deleteSource } from "../api/delete-source";

interface SourceTableProps {
  sources: NewsSourceDetail[];
}

export function SourceTable({ sources }: SourceTableProps) {
  const router = useRouter();
  const [toggling, setToggling] = useState<number | null>(null);

  async function handleToggle(source: NewsSourceDetail) {
    setToggling(source.id);
    try {
      const updated = source.isActive
        ? await deactivateSource(source.id)
        : await activateSource(source.id);
      toast.success(
        `${updated.name} ${updated.isActive ? "enabled" : "disabled"}`,
      );
      router.refresh();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.detail : "Failed to update source",
      );
    } finally {
      setToggling(null);
    }
  }

  async function handleDelete(id: number, name: string) {
    try {
      await deleteSource(id);
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
          <TableHead>Endpoint URL</TableHead>
          <TableHead className="text-center">Status</TableHead>
          <TableHead className="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sources.map((source) => (
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
                disabled={toggling === source.id}
                onCheckedChange={() => handleToggle(source)}
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
