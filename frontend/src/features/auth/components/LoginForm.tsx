"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useActionState, useEffect, useRef, useState } from "react";
import { z } from "zod";
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
import { signIn } from "@/lib/auth/auth-client";
import { parseLoginCallback } from "@/lib/auth/login-callback";
import { LoginSchema } from "../schemas/auth";

type LoginFieldErrors = Partial<Record<"email" | "password", string>>;

type SignInFailure = "invalid_credentials" | "rate_limited" | "unavailable";

type LoginState =
  | { status: "idle" }
  | {
      status: "error";
      fieldErrors: LoginFieldErrors;
      signInFailure?: SignInFailure;
    }
  | { status: "ok" };

const INITIAL_STATE: LoginState = { status: "idle" };

const SIGN_IN_FAILURE_MESSAGES: Record<SignInFailure, string> = {
  invalid_credentials: "メールアドレスまたはパスワードが正しくありません。",
  rate_limited:
    "ログインの試行回数が上限に達しました。しばらくしてから再度お試しください。",
  unavailable: "ログインできませんでした。時間をおいて再度お試しください。",
};

function classifySignInFailure(status: number): SignInFailure {
  if (status === 401) return "invalid_credentials";
  if (status === 429) return "rate_limited";
  return "unavailable";
}

async function action(
  _prev: LoginState,
  formData: FormData,
): Promise<LoginState> {
  const parsed = LoginSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    const { fieldErrors } = z.flattenError(parsed.error);
    // EOP 下で Partial<Record<...>> に undefined 明示代入はできないため、
    // 値ありフィールドのみ条件付き spread で組む。
    const result: LoginFieldErrors = {};
    if (fieldErrors.email?.[0] !== undefined)
      result.email = fieldErrors.email[0];
    if (fieldErrors.password?.[0] !== undefined)
      result.password = fieldErrors.password[0];
    return { status: "error", fieldErrors: result };
  }
  const { error } = await signIn.email(parsed.data);
  if (error) {
    // credential の内訳 (email 不在 vs password 違い) は出さず、応答の種類だけで文言を分ける。
    return {
      status: "error",
      fieldErrors: {},
      signInFailure: classifySignInFailure(error.status),
    };
  }
  return { status: "ok" };
}

export function LoginForm({
  returnTo = "/",
  requiresLoginReason = false,
  backHref = "/",
  backLabel = "ニュースへ戻る",
}: {
  returnTo?: string;
  requiresLoginReason?: boolean;
  backHref?: string;
  backLabel?: string;
}) {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);
  const emailRef = useRef<HTMLInputElement>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (state.status === "ok") {
      router.push(parseLoginCallback(returnTo) ?? "/");
      router.refresh();
    } else if (state.status === "error") {
      emailRef.current?.focus();
    }
  }, [state, router, returnTo]);

  const isError = state.status === "error";
  const emailError = isError ? state.fieldErrors.email : undefined;
  const passwordError = isError ? state.fieldErrors.password : undefined;
  const signInFailure = isError ? state.signInFailure : undefined;
  const formError = signInFailure
    ? SIGN_IN_FAILURE_MESSAGES[signInFailure]
    : undefined;
  // 認証情報の不一致だけを入力誤りとして両 input を invalid にする。
  const credentialsInvalid = signInFailure === "invalid_credentials";
  const emailInvalid = !!emailError || credentialsInvalid;
  const passwordInvalid = !!passwordError || credentialsInvalid;
  // 成功後もページを離れるまで送信中の表示を保ち、失敗したように見せない。
  const busy = pending || state.status === "ok";

  const emailDescribedBy =
    [emailError && "email-error", formError && "login-form-error"]
      .filter(Boolean)
      .join(" ") || undefined;
  const passwordDescribedBy =
    [passwordError && "password-error", formError && "login-form-error"]
      .filter(Boolean)
      .join(" ") || undefined;

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>
          <h1>ログイン</h1>
        </CardTitle>
        <CardDescription>
          登録済みのアカウントでログインしてください
        </CardDescription>
        {requiresLoginReason ? (
          <p className="text-sm text-muted-foreground" role="note">
            この機能の利用にはログインが必要です
          </p>
        ) : null}
      </CardHeader>
      <form action={formAction} aria-busy={busy}>
        <CardContent className="flex flex-col gap-4">
          <Alert role="note">
            <AlertTitle>招待制で運用しています</AlertTitle>
            <AlertDescription>
              現在、一般向けの新規登録は受け付けていません。
            </AlertDescription>
          </Alert>
          {formError && (
            <div
              id="login-form-error"
              role="alert"
              aria-live="polite"
              className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
            >
              {formError}
            </div>
          )}
          <div className="flex flex-col gap-2">
            <Label htmlFor="email">メールアドレス</Label>
            <Input
              ref={emailRef}
              id="email"
              name="email"
              type="email"
              placeholder="you@example.com"
              autoComplete="email"
              spellCheck={false}
              required
              disabled={busy}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              aria-invalid={emailInvalid || undefined}
              aria-describedby={emailDescribedBy}
            />
            {emailError && (
              <p
                id="email-error"
                role="alert"
                className="text-sm text-destructive"
              >
                {emailError}
              </p>
            )}
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="password">パスワード</Label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              disabled={busy}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={passwordInvalid || undefined}
              aria-describedby={passwordDescribedBy}
            />
            {passwordError && (
              <p
                id="password-error"
                role="alert"
                className="text-sm text-destructive"
              >
                {passwordError}
              </p>
            )}
          </div>
        </CardContent>
        <CardFooter className="flex flex-col gap-2">
          <Button type="submit" className="w-full" disabled={busy}>
            {busy ? (
              <>
                <Spinner data-icon="inline-start" aria-hidden="true" />
                <span role="status" aria-live="polite" aria-atomic="true">
                  ログイン中…
                </span>
              </>
            ) : (
              "ログイン"
            )}
          </Button>
          <Link
            href={backHref}
            className="text-center text-sm text-muted-foreground underline-offset-4 hover:underline"
          >
            {backLabel}
          </Link>
        </CardFooter>
      </form>
    </Card>
  );
}
