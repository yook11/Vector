"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useActionState, useEffect, useRef, useState } from "react";
import { z } from "zod";
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
import { signUp } from "@/lib/auth/auth-client";
import { passwordPolicy } from "@/lib/auth/auth-config";
import { RegisterSchema } from "../schemas/auth";

type RegisterField = "email" | "password";

type RegisterFieldErrors = Partial<Record<RegisterField, string>>;

type SignUpFailure = "rate_limited" | "unavailable";

type RegisterState =
  | { status: "idle" }
  | {
      status: "error";
      fieldErrors: RegisterFieldErrors;
      signUpFailure?: SignUpFailure;
    }
  | { status: "ok" };

const INITIAL_STATE: RegisterState = { status: "idle" };

// 入力欄に結び付く Better Auth の error code。422 は原因が複数あるため status では分けない。
const SIGN_UP_FIELD_ERRORS: Record<
  string,
  { field: RegisterField; message: string }
> = {
  USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL: {
    field: "email",
    message: "このメールアドレスは登録済みです。",
  },
  INVALID_EMAIL: {
    field: "email",
    message: "有効なメールアドレスを入力してください。",
  },
  PASSWORD_TOO_SHORT: {
    field: "password",
    message: `パスワードは${passwordPolicy.minLength}文字以上で入力してください。`,
  },
  PASSWORD_TOO_LONG: {
    field: "password",
    message: `パスワードは${passwordPolicy.maxLength}文字以内で入力してください。`,
  },
};

const SIGN_UP_FAILURE_MESSAGES: Record<SignUpFailure, string> = {
  rate_limited:
    "登録の試行回数が上限に達しました。しばらくしてから再度お試しください。",
  unavailable: "登録に失敗しました。時間をおいて再度お試しください。",
};

async function action(
  _prev: RegisterState,
  formData: FormData,
): Promise<RegisterState> {
  const parsed = RegisterSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    const { fieldErrors } = z.flattenError(parsed.error);
    const result: RegisterFieldErrors = {};
    if (fieldErrors.email?.[0] !== undefined)
      result.email = fieldErrors.email[0];
    if (fieldErrors.password?.[0] !== undefined)
      result.password = fieldErrors.password[0];
    return { status: "error", fieldErrors: result };
  }
  // name は画面にも backend にも使わないため、入力を求めず空文字で保存する。
  const { error } = await signUp.email({ ...parsed.data, name: "" });
  if (error) {
    const fieldError =
      error.code === undefined ? undefined : SIGN_UP_FIELD_ERRORS[error.code];
    if (fieldError) {
      return {
        status: "error",
        fieldErrors: { [fieldError.field]: fieldError.message },
      };
    }
    return {
      status: "error",
      fieldErrors: {},
      signUpFailure: error.status === 429 ? "rate_limited" : "unavailable",
    };
  }
  return { status: "ok" };
}

export function RegisterForm() {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (state.status === "ok") {
      router.push("/");
      router.refresh();
    } else if (state.status === "error") {
      const { fieldErrors } = state;
      const focusTarget =
        fieldErrors.password !== undefined && fieldErrors.email === undefined
          ? passwordRef
          : emailRef;
      focusTarget.current?.focus();
    }
  }, [state, router]);

  const isError = state.status === "error";
  const emailError = isError ? state.fieldErrors.email : undefined;
  const passwordError = isError ? state.fieldErrors.password : undefined;
  const signUpFailure = isError ? state.signUpFailure : undefined;
  const formError = signUpFailure
    ? SIGN_UP_FAILURE_MESSAGES[signUpFailure]
    : undefined;
  // 成功後もページを離れるまで送信中の表示を保ち、失敗したように見せない。
  const busy = pending || state.status === "ok";

  const emailDescribedBy =
    [emailError && "register-email-error", formError && "register-form-error"]
      .filter(Boolean)
      .join(" ") || undefined;
  const passwordDescribedBy = [
    "register-password-hint",
    passwordError && "register-password-error",
    formError && "register-form-error",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>
          <h1>新規登録</h1>
        </CardTitle>
        <CardDescription>
          メールアドレスとパスワードでアカウントを作成します
        </CardDescription>
      </CardHeader>
      <form action={formAction} aria-busy={busy}>
        <CardContent className="flex flex-col gap-4">
          {formError && (
            <div
              id="register-form-error"
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
              aria-invalid={emailError ? true : undefined}
              aria-describedby={emailDescribedBy}
            />
            {emailError && (
              <p
                id="register-email-error"
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
              ref={passwordRef}
              id="password"
              name="password"
              type="password"
              autoComplete="new-password"
              required
              disabled={busy}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={passwordError ? true : undefined}
              aria-describedby={passwordDescribedBy}
            />
            <p
              id="register-password-hint"
              className="text-sm text-muted-foreground"
            >
              {passwordPolicy.minLength}文字以上
            </p>
            {passwordError && (
              <p
                id="register-password-error"
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
                  登録中…
                </span>
              </>
            ) : (
              "アカウントを作成"
            )}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            アカウントをお持ちの方は{" "}
            <Link
              href="/auth/login"
              className="whitespace-nowrap text-foreground underline-offset-4 hover:underline"
            >
              ログイン
            </Link>
          </p>
        </CardFooter>
      </form>
    </Card>
  );
}
