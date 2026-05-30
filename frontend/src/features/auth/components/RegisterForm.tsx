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
import { signUp } from "@/lib/auth/auth-client";
import { RegisterSchema } from "../schemas/auth";
import type { SignUpError } from "./_auth-types";

// Better Auth signUp.email の既知エラーコードだけを
// ユーザ向け文言に変換する。
// 未知コードは generic 文言に丸め、内部実装語彙を出さない。
const REGISTER_ERROR_MESSAGES: Record<string, string> = {
  USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL:
    "An account with this email already exists",
  USER_ALREADY_EXISTS: "An account with this email already exists",
  PASSWORD_TOO_SHORT: "Password must be at least 8 characters",
  PASSWORD_TOO_LONG: "Password is too long",
  INVALID_EMAIL: "Please enter a valid email address",
};

const GENERIC_VALIDATION_MESSAGE = "Please check your input and try again";
const GENERIC_FAILURE_MESSAGE = "Registration failed. Please try again later.";

type RegisterFieldErrors = Partial<
  Record<"email" | "password" | "displayName", string>
>;

type RegisterState =
  | { status: "idle" }
  | {
      status: "error";
      fieldErrors: RegisterFieldErrors;
      formError?: string;
      // schema fail / Better Auth fail のどちらでも focus 先 field を一意化。
      focus: "email" | "password" | "displayName";
    }
  | { status: "ok" };

const INITIAL_STATE: RegisterState = { status: "idle" };

function resolveAuthError(authError: SignUpError): {
  fieldErrors: RegisterFieldErrors;
  formError?: string;
  focus: "email" | "password" | "displayName";
} {
  const code = authError.code ?? authError.error?.code;
  // noUncheckedIndexedAccess: true により Record<string, string>[code] は
  // string | undefined になるため、message を明示的に narrow する。
  const message =
    code !== undefined ? REGISTER_ERROR_MESSAGES[code] : undefined;
  if (code && message !== undefined) {
    if (
      code === "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL" ||
      code === "USER_ALREADY_EXISTS" ||
      code === "INVALID_EMAIL"
    ) {
      return { fieldErrors: { email: message }, focus: "email" };
    }
    if (code === "PASSWORD_TOO_SHORT" || code === "PASSWORD_TOO_LONG") {
      // password 系エラーは password field に紐付け、
      // focus 先も password に固定する。
      return { fieldErrors: { password: message }, focus: "password" };
    }
  }
  if (authError.status === 400 || authError.status === 422) {
    return {
      fieldErrors: {},
      formError: GENERIC_VALIDATION_MESSAGE,
      focus: "email",
    };
  }
  return {
    fieldErrors: {},
    formError: GENERIC_FAILURE_MESSAGE,
    focus: "email",
  };
}

async function action(
  _prev: RegisterState,
  formData: FormData,
): Promise<RegisterState> {
  const parsed = RegisterSchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    const { fieldErrors } = z.flattenError(parsed.error);
    // EOP 下で Partial<Record<...>> に undefined 明示代入はできないため、
    // 値ありフィールドのみ条件付きで組む。
    const fe: RegisterFieldErrors = {};
    if (fieldErrors.email?.[0] !== undefined) fe.email = fieldErrors.email[0];
    if (fieldErrors.password?.[0] !== undefined)
      fe.password = fieldErrors.password[0];
    if (fieldErrors.displayName?.[0] !== undefined)
      fe.displayName = fieldErrors.displayName[0];
    // focus 優先度: email > password > displayName (form の上から順)
    const focus: "email" | "password" | "displayName" = fe.email
      ? "email"
      : fe.password
        ? "password"
        : "displayName";
    return { status: "error", fieldErrors: fe, focus };
  }
  const { email, password, displayName } = parsed.data;
  // Better Auth required field. displayName 省略時は email local part にフォールバック。
  const name = displayName || email.split("@")[0] || email;
  const { error } = await signUp.email({ email, password, name });
  if (error) {
    const resolved = resolveAuthError(error);
    return { status: "error", ...resolved };
  }
  return { status: "ok" };
}

export function RegisterForm() {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(action, INITIAL_STATE);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const displayNameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (state.status === "ok") {
      router.push("/");
      router.refresh();
    } else if (state.status === "error") {
      const target =
        state.focus === "email"
          ? emailRef
          : state.focus === "password"
            ? passwordRef
            : displayNameRef;
      target.current?.focus();
    }
  }, [state, router]);

  const isError = state.status === "error";
  const emailError = isError ? state.fieldErrors.email : undefined;
  const passwordError = isError ? state.fieldErrors.password : undefined;
  const displayNameError = isError ? state.fieldErrors.displayName : undefined;
  const formError = isError ? state.formError : undefined;

  const fieldDescribedBy = (
    fieldErrorId: string | null,
    extra: string | null = null,
  ): string | undefined =>
    [fieldErrorId, formError && "register-form-error", extra]
      .filter(Boolean)
      .join(" ") || undefined;

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-2xl">Register</CardTitle>
        <CardDescription>Create your Vector account</CardDescription>
      </CardHeader>
      <form action={formAction}>
        <CardContent className="space-y-4">
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
          {/* displayName の入力ガイド。
              maxLength は UX 用で、検証本体は zod schema 側に集約する。 */}
          <div className="space-y-2">
            <Label htmlFor="displayName">Display Name</Label>
            <Input
              ref={displayNameRef}
              id="displayName"
              name="displayName"
              type="text"
              placeholder="表示名（任意）"
              autoComplete="nickname"
              spellCheck={false}
              maxLength={100}
              aria-invalid={!!displayNameError || !!formError || undefined}
              aria-describedby={fieldDescribedBy(
                displayNameError ? "displayname-error" : null,
                "displayname-help",
              )}
            />
            <p id="displayname-help" className="text-xs text-muted-foreground">
              英数字・日本語・スペース・ハイフン・アンダースコアのみ（最大100文字）
            </p>
            {displayNameError && (
              <p
                id="displayname-error"
                role="alert"
                className="text-sm text-destructive"
              >
                {displayNameError}
              </p>
            )}
          </div>
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
              aria-invalid={!!emailError || !!formError || undefined}
              aria-describedby={fieldDescribedBy(
                emailError ? "email-error" : null,
              )}
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
              ref={passwordRef}
              id="password"
              name="password"
              type="password"
              placeholder="Minimum 8 characters"
              autoComplete="new-password"
              required
              minLength={8}
              aria-invalid={!!passwordError || !!formError || undefined}
              aria-describedby={fieldDescribedBy(
                passwordError ? "password-error" : null,
              )}
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
            {pending ? "Creating account…" : "Create account"}
          </Button>
          <p className="text-sm text-muted-foreground">
            Already have an account?{" "}
            <Link
              href="/auth/login"
              className="underline hover:text-foreground"
            >
              Sign in
            </Link>
          </p>
        </CardFooter>
      </form>
    </Card>
  );
}
