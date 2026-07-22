"use client";

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
import { LoginSchema } from "../schemas/auth";

type LoginFieldErrors = Partial<Record<"email" | "password", string>>;

type LoginState =
  | { status: "idle" }
  | { status: "error"; fieldErrors: LoginFieldErrors; formError?: string }
  | { status: "ok" };

const INITIAL_STATE: LoginState = { status: "idle" };

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
    // sign-in 失敗の credential 内訳 (email 不在 vs password 違い) は frontend に
    // 出さない。formError に統合し、両 input を invalid 表示にする。
    return {
      status: "error",
      fieldErrors: {},
      formError: "メールアドレスまたはパスワードが正しくありません。",
    };
  }
  return { status: "ok" };
}

export function LoginForm() {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);
  const emailRef = useRef<HTMLInputElement>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (state.status === "ok") {
      router.push("/");
      router.refresh();
    } else if (state.status === "error") {
      emailRef.current?.focus();
    }
  }, [state, router]);

  const isError = state.status === "error";
  const emailError = isError ? state.fieldErrors.email : undefined;
  const passwordError = isError ? state.fieldErrors.password : undefined;
  const formError = isError ? state.formError : undefined;
  // formError は "credential 全体不正" の意味なので両 input を invalid とする。
  const emailInvalid = !!emailError || !!formError;
  const passwordInvalid = !!passwordError || !!formError;

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
      </CardHeader>
      <form action={formAction} aria-busy={pending}>
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
              disabled={pending}
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
              disabled={pending}
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
          <Button type="submit" className="w-full" disabled={pending}>
            {pending ? (
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
        </CardFooter>
      </form>
    </Card>
  );
}
