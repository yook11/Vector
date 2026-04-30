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
import {
  NewSourceSchema,
  type SourceType,
  SourceTypeSchema,
} from "../schemas/source";

interface SourceFormDialogProps {
  trigger: React.ReactNode;
}

type FormState =
  | { status: "idle" }
  | { status: "ok"; createdName: string }
  | { status: "error"; error: unknown; fallback: string };

const INITIAL_STATE: FormState = { status: "idle" };

// Next.js の redirect() / notFound() が投げる特殊 error を識別する。
// Next.js 16.2 では isRedirectError は公式 export されていないため digest
// プロパティで判定する (Next.js 13 から digest = "NEXT_REDIRECT;<status>;<url>"
// の構造で安定)。re-throw しないと caller の try/catch で navigation が
// 握り潰され、admin route 緩和時に未ログインユーザの login redirect が
// 機能しなくなる罠を構造的に塞ぐ。
function isRedirectError(err: unknown): boolean {
  return (
    err !== null &&
    typeof err === "object" &&
    "digest" in err &&
    typeof (err as { digest: unknown }).digest === "string" &&
    (err as { digest: string }).digest.startsWith("NEXT_REDIRECT")
  );
}

async function action(
  _prev: FormState,
  formData: FormData,
): Promise<FormState> {
  // SSoT: NewSourceSchema が SourceName / SafeUrl / SourceType の不変条件を
  // 表現する。Server Action 直叩き耐性 (defense-in-depth) も同 schema が担う。
  const parseResult = NewSourceSchema.safeParse(Object.fromEntries(formData));
  if (!parseResult.success) {
    const firstIssue = parseResult.error.issues[0];
    return {
      status: "error",
      error: parseResult.error,
      fallback: firstIssue?.message ?? "入力内容を確認してください",
    };
  }
  try {
    await createSource(parseResult.data);
    return { status: "ok", createdName: parseResult.data.name };
  } catch (err) {
    // redirect throw を握り潰さず再 throw して Next.js の navigation 経路に戻す。
    if (isRedirectError(err)) throw err;
    return {
      status: "error",
      error: err,
      fallback: "ソースの追加に失敗しました",
    };
  }
}

// SourceForm を分離する目的:
// 1. Radix Dialog の DialogContent は open=false で unmount される。内部の
//    useState / useActionState は dialog open ごとに新規 mount で fresh に
//    作り直されるため、useEffect([open]) での sourceType reset と、再 open 時
//    に前回 ok/error 状態を toast 再発火する罠が構造的に消える。
// 2. SourceFormDialog 親は open state のみを持つ薄い wrapper になる。
function SourceForm({ onSuccess }: { onSuccess: () => void }) {
  const [sourceType, setSourceType] = useState<SourceType>("rss");
  const nameRef = useRef<HTMLInputElement>(null);
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);

  // Strict Mode は dev で useEffect を 2 回実行する。useActionState が action
  // 完了ごとに新規オブジェクトを返す仕様を利用し、state identity の変化を
  // 検出した最初の 1 回だけ後処理を発火する。idle → ok / error → ok のいずれの
  // transition も identity 変化として捉えられる。
  const lastFiredStateRef = useRef<FormState>(state);
  useEffect(() => {
    if (state === lastFiredStateRef.current) return;
    lastFiredStateRef.current = state;
    if (state.status === "ok") {
      toast.success(`Added "${state.createdName}"`);
      onSuccess();
    } else if (state.status === "error") {
      toastError(state.error, state.fallback);
      nameRef.current?.focus();
    }
  }, [state, onSuccess]);

  return (
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
          onValueChange={(v) => {
            // Radix Select は value を string で渡してくる。SourceTypeSchema
            // で narrow して `as` cast を避ける (SourceType の SSoT 経路維持)。
            const parsed = SourceTypeSchema.safeParse(v);
            if (parsed.success) setSourceType(parsed.data);
          }}
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
  );
}

export function SourceFormDialog({ trigger }: SourceFormDialogProps) {
  const [open, setOpen] = useState(false);

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
        <SourceForm onSuccess={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  );
}
