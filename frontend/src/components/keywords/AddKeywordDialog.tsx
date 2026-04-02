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

interface CategoryOption {
  slug: string;
  name: string;
}

interface AddKeywordDialogProps {
  keywordCategories?: CategoryOption[];
}

export function AddKeywordDialog({ keywordCategories }: AddKeywordDialogProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) {
      setName("");
      setSelectedSlug(null);
    }
  }, [open]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || selectedSlug === null) return;

    setLoading(true);
    try {
      await createKeyword({
        name: trimmed,
        categorySlug: selectedSlug,
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
                    key={cat.slug}
                    type="button"
                    size="sm"
                    variant={selectedSlug === cat.slug ? "default" : "outline"}
                    onClick={() => setSelectedSlug(cat.slug)}
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
              disabled={loading || !name.trim() || selectedSlug === null}
            >
              {loading ? "Adding..." : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
