import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export const metadata = {
  title: "招待制 - Vector",
};

export default function RegisterPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-2xl">招待制で運用しています</CardTitle>
          <CardDescription>
            現在、一般向けの新規登録は受け付けていません。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            アカウントをお持ちの方はログインしてください。
          </p>
        </CardContent>
        <CardFooter>
          <Button asChild className="w-full">
            <Link href="/auth/login">ログイン</Link>
          </Button>
        </CardFooter>
      </Card>
    </main>
  );
}
