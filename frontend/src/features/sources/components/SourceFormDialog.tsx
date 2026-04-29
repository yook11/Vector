"use client";

import { useActionState, useEffect, useRef, useState } from "react";
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

type FormState =
  | { status: "idle" }
  | { status: "ok"; createdName: string }
  | { status: "error"; error: unknown; fallback: string };

const INITIAL_STATE: FormState = { status: "idle" };

async function action(
  _prev: FormState,
  formData: FormData,
): Promise<FormState> {
  const name = String(formData.get("name") ?? "").trim();
  if (!name) {
    return {
      status: "error",
      error: new Error("Name is required"),
      fallback: "Name is required",
    };
  }
  try {
    await createSource({
      name,
      sourceType: (formData.get("sourceType") as "rss" | "api") ?? "rss",
      siteUrl: String(formData.get("siteUrl") ?? "").trim(),
      endpointUrl: String(formData.get("endpointUrl") ?? "").trim(),
    });
    return { status: "ok", createdName: name };
  } catch (err) {
    return {
      status: "error",
      error: err,
      fallback: "ソースの追加に失敗しました",
    };
  }
}

export function SourceFormDialog({ trigger }: SourceFormDialogProps) {
  const [open, setOpen] = useState(false);
  const [sourceType, setSourceType] = useState<"rss" | "api">("rss");
  const nameRef = useRef<HTMLInputElement>(null);
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);

  useEffect(() => {
    if (state.status === "ok") {
      toast.success(`Added "${state.createdName}"`);
      setOpen(false);
      // createSource 内で revalidateTag("sources") 済 → router.refresh() 不要
    } else if (state.status === "error") {
      toastError(state.error, state.fallback);
      nameRef.current?.focus();
    }
  }, [state]);

  // Dialog close で DialogContent (form 含む) は unmount されるため input は
  // 自然 reset されるが、sourceType は親 Component で保持しているため open 切替時に
  // 明示リセットする。
  useEffect(() => {
    if (open) setSourceType("rss");
  }, [open]);

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
        <form action={formAction} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="source-name">Name</Label>
            <Input
              ref={nameRef}
              id="source-name"
              name="name"
              defaultValue=""
              placeholder="e.g. TechCrunch"
              spellCheck={false}
              maxLength={50}
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="source-type">Type</Label>
            <Select
              name="sourceType"
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
              name="endpointUrl"
              type="url"
              defaultValue=""
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
              name="siteUrl"
              type="url"
              defaultValue=""
              placeholder="https://example.com"
              autoComplete="url"
              spellCheck={false}
              required
            />
          </div>

          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending ? "Saving…" : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
