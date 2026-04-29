"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";

interface ErrorMessageProps {
  title: string;
  description: string;
  error: Error & { digest?: string };
  reset: () => void;
}

export function ErrorMessage({
  title,
  description,
  error,
  reset,
}: ErrorMessageProps) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-16 flex flex-col items-center justify-center gap-4">
        <h1 className="text-base font-medium">{title}</h1>
        <p className="text-sm text-muted-foreground">{description}</p>
        <Button variant="outline" size="sm" onClick={reset}>
          再試行
        </Button>
      </div>
    </main>
  );
}
