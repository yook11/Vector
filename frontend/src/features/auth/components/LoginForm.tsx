"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useActionState, useEffect, useRef } from "react";
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
    return {
      status: "error",
      fieldErrors: {
        email: fieldErrors.email?.[0],
        password: fieldErrors.password?.[0],
      },
    };
  }
  const { error } = await signIn.email(parsed.data);
  if (error) {
    // sign-in 失敗の credential 内訳 (email 不在 vs password 違い) は frontend に
    // 出さない。formError に統合し、両 input を invalid 表示にする。
    return {
      status: "error",
      fieldErrors: {},
      formError: "Invalid email or password",
    };
  }
  return { status: "ok" };
}

export function LoginForm() {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);
  const emailRef = useRef<HTMLInputElement>(null);

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
        <CardTitle className="text-2xl">Login</CardTitle>
        <CardDescription>Sign in to your Vector account</CardDescription>
      </CardHeader>
      <form action={formAction}>
        <CardContent className="space-y-4">
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
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input
              ref={emailRef}
              id="email"
              name="email"
              type="email"
              placeholder="you@example.com"
              autoComplete="email"
              spellCheck={false}
              required
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
          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
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
            {pending ? "Signing in…" : "Sign in"}
          </Button>
          <p className="text-sm text-muted-foreground">
            Don’t have an account?{" "}
            <Link
              href="/auth/register"
              className="underline hover:text-foreground"
            >
              Register
            </Link>
          </p>
        </CardFooter>
      </form>
    </Card>
  );
}
