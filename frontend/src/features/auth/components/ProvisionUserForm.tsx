"use client";

import { ClipboardCopy } from "lucide-react";
import {
  useActionState,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { passwordPolicy } from "@/lib/auth/auth-config";
import { type ProvisionUserState, provisionUser } from "../api/provision-user";

type ProvisionUserField = "name" | "email" | "password";

const INITIAL_STATE: ProvisionUserState = { status: "idle" };

function describedBy(
  field: ProvisionUserField,
  hasFieldError: boolean,
  hasFormError: boolean,
) {
  const ids = [
    hasFieldError ? `provision-${field}-error` : undefined,
    hasFormError ? "provision-form-error" : undefined,
  ].filter((id): id is string => id !== undefined);

  return ids.length > 0 ? ids.join(" ") : undefined;
}

export function ProvisionUserForm() {
  const [state, formAction, pending] = useActionState(
    provisionUser,
    INITIAL_STATE,
  );
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [credentialsRecorded, setCredentialsRecorded] = useState(false);
  const credentialsRecordedRef = useRef<HTMLInputElement>(null);
  const [copyFeedback, setCopyFeedback] = useState<
    "copied" | "failed" | undefined
  >(undefined);

  useEffect(() => {
    if (state.status !== "success") return;

    setName("");
    setEmail("");
    setPassword("");
    setCredentialsRecorded(false);
    setCopyFeedback(undefined);
  }, [state]);

  useLayoutEffect(() => {
    if (state.status === "error" && credentialsRecordedRef.current) {
      credentialsRecordedRef.current.checked = credentialsRecorded;
    }
  }, [credentialsRecorded, state]);

  const isError = state.status === "error";
  const formError = isError ? state.formError : undefined;
  const fieldErrors = isError ? state.fieldErrors : undefined;

  async function copyCredentials() {
    try {
      await navigator.clipboard.writeText(`${email}\n${password}`);
      setCopyFeedback("copied");
    } catch {
      setCopyFeedback("failed");
    }
  }

  function updateEmail(value: string) {
    setEmail(value);
    setCredentialsRecorded(false);
    setCopyFeedback(undefined);
  }

  function updatePassword(value: string) {
    setPassword(value);
    setCredentialsRecorded(false);
    setCopyFeedback(undefined);
  }

  return (
    <Card className="w-full max-w-xl">
      <CardHeader className="gap-3 border-b border-border/70">
        <CardTitle className="text-xl tracking-tight">
          デモユーザーを登録
        </CardTitle>
        <CardDescription className="leading-relaxed">
          認証情報を控えてから、一般ユーザーのアカウントを発行します。
        </CardDescription>
      </CardHeader>
      <form
        action={formAction}
        aria-busy={pending}
        noValidate
        onReset={(event) => event.preventDefault()}
      >
        <CardContent className="flex flex-col gap-6 pt-6">
          {state.status === "success" ? (
            <Alert role="status" aria-live="polite" aria-atomic="true">
              <AlertTitle>一般ユーザーを登録しました</AlertTitle>
              <AlertDescription>{state.email}</AlertDescription>
            </Alert>
          ) : null}

          {formError ? (
            <Alert
              id="provision-form-error"
              variant="destructive"
              aria-live="polite"
            >
              <AlertTitle>登録できませんでした</AlertTitle>
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          ) : null}

          <Alert role="note">
            <AlertTitle>認証情報の取り扱い</AlertTitle>
            <AlertDescription>
              登録後にパスワードを再表示することはできません。利用者へ共有する前に、必ず安全な場所へ控えてください。
            </AlertDescription>
          </Alert>

          <div className="grid gap-5 sm:grid-cols-2">
            <div className="flex flex-col gap-2 sm:col-span-2">
              <Label htmlFor="provision-name">名前</Label>
              <Input
                id="provision-name"
                name="name"
                type="text"
                autoComplete="name"
                maxLength={100}
                required
                disabled={pending}
                value={name}
                onChange={(event) => setName(event.target.value)}
                aria-invalid={
                  Boolean(fieldErrors?.name || formError) || undefined
                }
                aria-describedby={describedBy(
                  "name",
                  fieldErrors?.name !== undefined,
                  formError !== undefined,
                )}
              />
              {fieldErrors?.name ? (
                <p
                  id="provision-name-error"
                  role="alert"
                  className="text-sm text-destructive"
                >
                  {fieldErrors.name}
                </p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="provision-email">メールアドレス</Label>
              <Input
                id="provision-email"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="demo@example.com"
                spellCheck={false}
                required
                disabled={pending}
                value={email}
                onChange={(event) => updateEmail(event.target.value)}
                aria-invalid={
                  Boolean(fieldErrors?.email || formError) || undefined
                }
                aria-describedby={describedBy(
                  "email",
                  fieldErrors?.email !== undefined,
                  formError !== undefined,
                )}
              />
              {fieldErrors?.email ? (
                <p
                  id="provision-email-error"
                  role="alert"
                  className="text-sm text-destructive"
                >
                  {fieldErrors.email}
                </p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="provision-password">パスワード</Label>
              <Input
                id="provision-password"
                name="password"
                type="password"
                autoComplete="new-password"
                minLength={passwordPolicy.minLength}
                maxLength={passwordPolicy.maxLength}
                required
                disabled={pending}
                value={password}
                onChange={(event) => updatePassword(event.target.value)}
                aria-invalid={
                  Boolean(fieldErrors?.password || formError) || undefined
                }
                aria-describedby={describedBy(
                  "password",
                  fieldErrors?.password !== undefined,
                  formError !== undefined,
                )}
              />
              {fieldErrors?.password ? (
                <p
                  id="provision-password-error"
                  role="alert"
                  className="text-sm text-destructive"
                >
                  {fieldErrors.password}
                </p>
              ) : null}
            </div>
          </div>

          <div className="flex flex-col gap-3 rounded-lg border border-border/70 bg-muted/30 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-1">
              <p className="text-sm font-medium">認証情報を控える</p>
              <p className="text-sm text-muted-foreground">
                コピーする内容はメールアドレスとパスワードのみです。
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={copyCredentials}
            >
              <ClipboardCopy data-icon="inline-start" aria-hidden="true" />
              認証情報をコピー
            </Button>
          </div>

          {copyFeedback === "copied" ? (
            <p
              role="status"
              aria-live="polite"
              className="text-sm text-muted-foreground"
            >
              認証情報をコピーしました。
            </p>
          ) : null}
          {copyFeedback === "failed" ? (
            <Alert variant="destructive" aria-live="polite">
              <AlertTitle>コピーできませんでした</AlertTitle>
              <AlertDescription>
                手動で認証情報を控えてから確認してください。
              </AlertDescription>
            </Alert>
          ) : null}

          <div className="flex items-start gap-3 rounded-md border border-border/70 px-3 py-3">
            <input
              ref={credentialsRecordedRef}
              id="credentials-recorded"
              type="checkbox"
              checked={credentialsRecorded}
              disabled={pending}
              onChange={(event) => setCredentialsRecorded(event.target.checked)}
              className="mt-0.5 size-4 shrink-0 accent-primary"
            />
            <div className="flex flex-col gap-1">
              <Label htmlFor="credentials-recorded">認証情報を控えました</Label>
              <p className="text-sm text-muted-foreground">
                メールアドレスまたはパスワードを変更すると、再度確認が必要です。
              </p>
            </div>
          </div>
        </CardContent>
        <CardFooter className="border-t border-border/70 pt-6">
          <Button
            type="submit"
            className="w-full sm:w-auto"
            disabled={pending || !credentialsRecorded}
          >
            {pending ? (
              <>
                <Spinner data-icon="inline-start" aria-hidden="true" />
                <span role="status" aria-live="polite" aria-atomic="true">
                  登録中…
                </span>
              </>
            ) : (
              "一般ユーザーを登録"
            )}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}
