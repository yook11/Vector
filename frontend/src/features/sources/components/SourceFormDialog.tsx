"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toastError } from "@/lib/utils/toast-error";
import { createSource } from "../api/create-source";

interface SourceFormDialogProps {
  trigger: React.ReactNode;
}

export function SourceFormDialog({ trigger }: SourceFormDialogProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const [name, setName] = useState("");
  const [sourceType, setSourceType] = useState<"rss" | "api">("rss");
  const [endpointUrl, setEndpointUrl] = useState("");
  const [siteUrl, setSiteUrl] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setSourceType("rss");
      setEndpointUrl("");
      setSiteUrl("");
    }
  }, [open]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;

    setLoading(true);
    try {
      await createSource({
        name: name.trim(),
        sourceType,
        siteUrl: siteUrl.trim(),
        endpointUrl: endpointUrl.trim(),
      });
      toast.success(`Added "${name.trim()}"`);
      setOpen(false);
      // Server Action 内で revalidateTag("sources") 済み → router.refresh() 不要
    } catch (err) {
      toastError(err, "ソースの追加に失敗しました");
      nameRef.current?.focus();
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add Source</DialogTitle>
          <DialogDescription className="sr-only">
            新しい RSS / API ソースを追加します
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="source-name">Name</Label>
            <Input
              ref={nameRef}
              id="source-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. TechCrunch"
              spellCheck={false}
              maxLength={50}
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="source-type">Type</Label>
            <Select
              value={sourceType}
              onValueChange={(v) => setSourceType(v as "rss" | "api")}
            >
              <SelectTrigger id="source-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="rss">RSS</SelectItem>
                <SelectItem value="api">API</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="endpoint-url">Endpoint URL</Label>
            <Input
              id="endpoint-url"
              type="url"
              value={endpointUrl}
              onChange={(e) => setEndpointUrl(e.target.value)}
              placeholder={
                sourceType === "rss"
                  ? "https://example.com/feed/"
                  : "https://api.example.com/v1/endpoint"
              }
              autoComplete="url"
              spellCheck={false}
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="site-url">Site URL</Label>
            <Input
              id="site-url"
              type="url"
              value={siteUrl}
              onChange={(e) => setSiteUrl(e.target.value)}
              placeholder="https://example.com"
              autoComplete="url"
              spellCheck={false}
              required
            />
          </div>

          <DialogFooter>
            <Button type="submit" disabled={loading || !name.trim()}>
              {loading ? "Saving…" : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
