import { cn } from "@/lib/utils/cn";

type PageContainerProps = {
  maxWidth?: "3xl" | "4xl" | "5xl";
  gap?: 8 | 10 | 12;
  children: React.ReactNode;
  className?: string;
};

const widthClassMap = {
  "3xl": "max-w-3xl",
  "4xl": "max-w-4xl",
  "5xl": "max-w-5xl",
} as const;

const gapClassMap = {
  8: "gap-8",
  10: "gap-10",
  12: "gap-12",
} as const;

export function PageContainer({
  maxWidth = "5xl",
  gap = 8,
  children,
  className,
}: PageContainerProps) {
  return (
    <main className="h-full overflow-y-auto">
      <div
        className={cn(
          "mx-auto px-8 sm:px-12 py-6 sm:py-8 flex flex-col",
          widthClassMap[maxWidth],
          gapClassMap[gap],
          className,
        )}
      >
        {children}
      </div>
    </main>
  );
}
