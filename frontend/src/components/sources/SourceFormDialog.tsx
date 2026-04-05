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
import { ApiError, clientCreateSource } from "@/lib/client-api";

interface SourceFormDialogProps {
  trigger: React.ReactNode;
}

export function SourceFormDialog({ trigger }: SourceFormDialogProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const [name, setName] = useState("");
  const [sourceType, setSourceType] = useState<"rss" | "api">("rss");
  const [endpointUrl, setEndpointUrl] = useState("");
  const [siteUrl, setSiteUrl] = useState("");

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
      await clientCreateSource({
        name: name.trim(),
        sourceType,
        siteUrl: siteUrl.trim(),
        endpointUrl: endpointUrl.trim(),
      });
      toast.success(`Added "${name.trim()}"`);
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
          <DialogTitle>Add Source</DialogTitle>
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
              {loading ? "Saving..." : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
