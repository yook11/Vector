"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
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

// Better Auth signUp.email が返す既知エラーコード → ユーザ向け固定文言。
// allowlist 設計: 未知コードや未来の変更で `authError.message` を素のまま
// 表示すると、内部実装語彙の漏洩や文言の不安定化が起きるため、ここに載って
// いないものはすべて generic 文言に丸める。
//
// 既知コードの根拠: node_modules/@better-auth/core/dist/error/codes.mjs
// USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL は status 422 で返る (旧コードの 409 判定は dead)
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

interface AuthErrorLike {
  status?: number;
  code?: string;
  error?: { code?: string };
}

function resolveRegisterError(authError: AuthErrorLike): {
  message: string;
  field: "email" | "displayName";
} {
  const code = authError.code ?? authError.error?.code;
  const known = code ? REGISTER_ERROR_MESSAGES[code] : undefined;
  if (code && known) {
    const isEmailIssue =
      code === "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL" ||
      code === "USER_ALREADY_EXISTS" ||
      code === "INVALID_EMAIL";
    return { message: known, field: isEmailIssue ? "email" : "displayName" };
  }
  if (authError.status === 400 || authError.status === 422) {
    return { message: GENERIC_VALIDATION_MESSAGE, field: "displayName" };
  }
  return { message: GENERIC_FAILURE_MESSAGE, field: "displayName" };
}

export function RegisterForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [isPending, setIsPending] = useState(false);
  const displayNameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setIsPending(true);

    const formData = new FormData(e.currentTarget);
    const parsed = RegisterSchema.safeParse({
      email: formData.get("email") ?? "",
      password: formData.get("password") ?? "",
      displayName: formData.get("displayName") ?? "",
    });

    if (!parsed.success) {
      setError(GENERIC_VALIDATION_MESSAGE);
      setIsPending(false);
      emailRef.current?.focus();
      return;
    }

    const { email, password, displayName } = parsed.data;

    // name: Better Auth required field — fallback to email local part, then to
    // raw email (`split("@")[0]` の戻り値型は `string | undefined` だが、type=email
    // を通った文字列で undefined にはならない; 型システム上の安全のため email を保険に)
    const name = displayName || email.split("@")[0] || email;

    const { error: authError } = await signUp.email({
      email,
      password,
      name,
    });

    setIsPending(false);

    if (authError) {
      const { message, field } = resolveRegisterError(authError);
      setError(message);
      if (field === "email") {
        emailRef.current?.focus();
      } else {
        displayNameRef.current?.focus();
      }
    } else {
      router.push("/");
      router.refresh();
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-2xl">Register</CardTitle>
        <CardDescription>Create your Vector account</CardDescription>
      </CardHeader>
      <form onSubmit={handleSubmit}>
        <CardContent className="space-y-4">
          {error && (
            <div
              id="register-error"
              role="alert"
              aria-live="polite"
              className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
            >
              {error}
            </div>
          )}
          {/* --- XSS対策 Step 1: フロントエンド側の入力ガイド ---
               ホワイトリストで制限している以上、何が使えるかをユーザーに伝える。
               maxLength / pattern はブラウザのネイティブバリデーション。
               ただしこれらはUXのためであり、セキュリティの本体はバックエンド側。
               攻撃者はブラウザを経由せず直接APIを叩けるため。 */}
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
              pattern="[\w\s\-]+"
              title="使用できる文字: 英数字、日本語、スペース、ハイフン、アンダースコア"
              aria-describedby={
                error ? "register-error displayname-help" : "displayname-help"
              }
              aria-invalid={error ? true : undefined}
            />
            <p id="displayname-help" className="text-xs text-muted-foreground">
              英数字・日本語・スペース・ハイフン・アンダースコアのみ（最大100文字）
            </p>
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
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "register-error" : undefined}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              name="password"
              type="password"
              placeholder="Minimum 8 characters"
              autoComplete="new-password"
              required
              minLength={8}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "register-error" : undefined}
            />
          </div>
        </CardContent>
        <CardFooter className="flex flex-col gap-2">
          <Button type="submit" className="w-full" disabled={isPending}>
            {isPending ? "Creating account…" : "Create account"}
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
