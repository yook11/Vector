"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
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
import { signUp } from "@/lib/auth-client";

export function RegisterForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [isPending, setIsPending] = useState(false);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setIsPending(true);

    const formData = new FormData(e.currentTarget);
    const email = formData.get("email") as string;
    const password = formData.get("password") as string;
    const displayName = (formData.get("displayName") as string) || undefined;

    // name: Better Auth required field — fallback to email local part
    const name = displayName || email.split("@")[0];

    const { error: authError } = await signUp.email({
      email,
      password,
      name,
    });

    setIsPending(false);

    if (authError) {
      if (authError.status === 409) {
        setError("An account with this email already exists");
      } else {
        setError(authError.message ?? "Registration failed");
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
            <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
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
              id="displayName"
              name="displayName"
              type="text"
              placeholder="表示名（任意）"
              maxLength={100}
              pattern="[\w\s\-]+"
              title="使用できる文字: 英数字、日本語、スペース、ハイフン、アンダースコア"
            />
            <p className="text-xs text-muted-foreground">
              英数字・日本語・スペース・ハイフン・アンダースコアのみ（最大100文字）
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              name="email"
              type="email"
              placeholder="you@example.com"
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              name="password"
              type="password"
              placeholder="Minimum 8 characters"
              required
              minLength={8}
            />
          </div>
        </CardContent>
        <CardFooter className="flex flex-col gap-2">
          <Button type="submit" className="w-full" disabled={isPending}>
            {isPending ? "Creating account..." : "Create account"}
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
