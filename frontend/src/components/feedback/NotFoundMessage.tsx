import { PendingAwareLink } from "@/components/layout/PageNavigation";
import { Button } from "@/components/ui/button";

export function NotFoundMessage({ message }: { message: string }) {
  return (
    <main className="flex flex-col items-center justify-center min-h-[50vh] gap-4">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="text-muted-foreground">{message}</p>
      <Button asChild>
        <PendingAwareLink href="/">Back to Dashboard</PendingAwareLink>
      </Button>
    </main>
  );
}
