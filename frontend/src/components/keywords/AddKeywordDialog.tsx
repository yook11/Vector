"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  clientCreateKeyword as createKeyword,
} from "@/lib/client-api";
import type { CategoryDetailResponse } from "@/types";

interface AddKeywordDialogProps {
  keywordCategories?: CategoryDetailResponse[];
}

export function AddKeywordDialog({ keywordCategories }: AddKeywordDialogProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(
    null,
  );
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) {
      setName("");
      setSelectedCategoryId(null);
    }
  }, [open]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || selectedCategoryId === null) return;

    setLoading(true);
    try {
      await createKeyword({
        name: trimmed,
        categoryId: selectedCategoryId,
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
            <Label htmlFor="keyword-name">Keyword</Label>
            <Input
              id="keyword-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. quantum computing"
              required
            />
          </div>
          {keywordCategories && keywordCategories.length > 0 && (
            <div className="space-y-2">
              <Label>Category (required)</Label>
              <div className="flex flex-wrap gap-2">
                {keywordCategories.map((cat) => (
                  <Button
                    key={cat.id}
                    type="button"
                    size="sm"
                    variant={
                      selectedCategoryId === cat.id ? "default" : "outline"
                    }
                    onClick={() => setSelectedCategoryId(cat.id)}
                  >
                    {cat.name}
                  </Button>
                ))}
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              type="submit"
              disabled={loading || !name.trim() || selectedCategoryId === null}
            >
              {loading ? "Adding..." : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
