import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function NewsNotFound() {
  return (
    <main className="flex flex-col items-center justify-center min-h-[50vh] gap-4">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="text-muted-foreground">Article not found.</p>
      <Button asChild>
        <Link href="/">Back to Dashboard</Link>
      </Button>
    </main>
  );
}
