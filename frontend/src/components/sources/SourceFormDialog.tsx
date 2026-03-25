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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ApiError,
  clientCreateSource,
  clientUpdateSource,
} from "@/lib/client-api";
import type { NewsSourceResponse } from "@/types";

interface SourceFormDialogProps {
  source?: NewsSourceResponse;
  trigger: React.ReactNode;
}

export function SourceFormDialog({ source, trigger }: SourceFormDialogProps) {
  const router = useRouter();
  const isEdit = !!source;
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const [name, setName] = useState("");
  const [sourceType, setSourceType] = useState<"rss" | "api">("rss");
  const [endpointUrl, setEndpointUrl] = useState("");
  const [siteUrl, setSiteUrl] = useState("");

  useEffect(() => {
    if (open && source) {
      setName(source.name);
      setSourceType(source.sourceType);
      setEndpointUrl(source.endpointUrl);
      setSiteUrl(source.siteUrl);
    } else if (open && !source) {
      setName("");
      setSourceType("rss");
      setEndpointUrl("");
      setSiteUrl("");
    }
  }, [open, source]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;

    setLoading(true);
    try {
      const body = {
        name: name.trim(),
        sourceType,
        siteUrl: siteUrl.trim(),
        endpointUrl: endpointUrl.trim(),
      };

      if (isEdit && source) {
        await clientUpdateSource(source.id, body);
        toast.success(`Updated "${name.trim()}"`);
      } else {
        await clientCreateSource(body);
        toast.success(`Added "${name.trim()}"`);
      }
      setOpen(false);
      router.refresh();
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : "Operation failed";
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Source" : "Add Source"}</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="source-name">Name</Label>
            <Input
              id="source-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. TechCrunch"
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
              required
            />
          </div>

          <DialogFooter>
            <Button type="submit" disabled={loading || !name.trim()}>
              {loading ? "Saving..." : isEdit ? "Save" : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
