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
  const [sourceType, setSourceType] = useState("rss");
  const [feedUrl, setFeedUrl] = useState("");
  const [apiEndpoint, setApiEndpoint] = useState("");
  const [siteUrl, setSiteUrl] = useState("");
  const [fetchInterval, setFetchInterval] = useState("720");

  useEffect(() => {
    if (open && source) {
      setName(source.name);
      setSourceType(source.sourceType);
      setFeedUrl(source.feedUrl ?? "");
      setApiEndpoint(source.apiEndpoint ?? "");
      setSiteUrl(source.siteUrl ?? "");
      setFetchInterval(String(source.fetchIntervalMinutes));
    } else if (open && !source) {
      setName("");
      setSourceType("rss");
      setFeedUrl("");
      setApiEndpoint("");
      setSiteUrl("");
      setFetchInterval("720");
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
        siteUrl: siteUrl.trim() || null,
        feedUrl: sourceType === "rss" ? feedUrl.trim() || null : null,
        apiEndpoint: sourceType === "api" ? apiEndpoint.trim() || null : null,
        fetchIntervalMinutes: Number(fetchInterval),
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
              placeholder="e.g. TechCrunch AI"
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="source-type">Type</Label>
            <Select value={sourceType} onValueChange={setSourceType}>
              <SelectTrigger id="source-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="rss">RSS</SelectItem>
                <SelectItem value="api">API</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {sourceType === "rss" ? (
            <div className="space-y-2">
              <Label htmlFor="feed-url">Feed URL</Label>
              <Input
                id="feed-url"
                value={feedUrl}
                onChange={(e) => setFeedUrl(e.target.value)}
                placeholder="https://example.com/feed/"
                required
              />
            </div>
          ) : (
            <div className="space-y-2">
              <Label htmlFor="api-endpoint">API Endpoint</Label>
              <Input
                id="api-endpoint"
                value={apiEndpoint}
                onChange={(e) => setApiEndpoint(e.target.value)}
                placeholder="e.g. hacker-news"
                required
              />
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="site-url">Site URL (optional)</Label>
            <Input
              id="site-url"
              value={siteUrl}
              onChange={(e) => setSiteUrl(e.target.value)}
              placeholder="https://example.com"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="fetch-interval">Fetch Interval (minutes)</Label>
            <Select value={fetchInterval} onValueChange={setFetchInterval}>
              <SelectTrigger id="fetch-interval">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="60">Every hour</SelectItem>
                <SelectItem value="180">Every 3 hours</SelectItem>
                <SelectItem value="360">Every 6 hours</SelectItem>
                <SelectItem value="720">Every 12 hours</SelectItem>
                <SelectItem value="1440">Once a day</SelectItem>
              </SelectContent>
            </Select>
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
