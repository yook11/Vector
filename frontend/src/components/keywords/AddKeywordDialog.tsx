"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  ApiError,
  clientCreateKeyword as createKeyword,
} from "@/lib/client-api";
import type { KeywordCategoryDetailResponse } from "@/types";

interface AddKeywordDialogProps {
  keywordCategories?: KeywordCategoryDetailResponse[];
}

export function AddKeywordDialog({ keywordCategories }: AddKeywordDialogProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [selectedCategoryIds, setSelectedCategoryIds] = useState<Set<number>>(
    new Set(),
  );
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) {
      setKeyword("");
      setSelectedCategoryIds(new Set());
    }
  }, [open]);

  function toggleCategory(id: number) {
    setSelectedCategoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = keyword.trim();
    if (!trimmed) return;

    setLoading(true);
    try {
      await createKeyword({
        keyword: trimmed,
        categoryIds: Array.from(selectedCategoryIds),
      });
      toast.success(`Added "${trimmed}"`);
      setOpen(false);
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        toast.error("Keyword already exists");
      } else {
        toast.error("Failed to add keyword");
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>Add Keyword</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add Keyword</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="keyword">Keyword</Label>
            <Input
              id="keyword"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="e.g. quantum computing"
              required
            />
          </div>
          {keywordCategories && keywordCategories.length > 0 && (
            <div className="space-y-2">
              <Label>Categories (optional)</Label>
              <div className="flex flex-wrap gap-2">
                {keywordCategories.map((cat) => (
                  <Button
                    key={cat.id}
                    type="button"
                    size="sm"
                    variant={
                      selectedCategoryIds.has(cat.id)
                        ? "default"
                        : "outline"
                    }
                    onClick={() => toggleCategory(cat.id)}
                  >
                    {cat.name}
                  </Button>
                ))}
              </div>
            </div>
          )}
          <DialogFooter>
            <Button type="submit" disabled={loading || !keyword.trim()}>
              {loading ? "Adding..." : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
