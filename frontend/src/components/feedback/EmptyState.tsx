import { cn } from "@/lib/utils/cn";

type EmptyStateProps = {
  title: string;
  description?: React.ReactNode;
  className?: string;
};

export function EmptyState({ title, description, className }: EmptyStateProps) {
  return (
    <div
      role="status"
      className={cn(
        "flex flex-col items-center justify-center py-16 text-muted-foreground",
        className,
      )}
    >
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="text-xs mt-1">{description}</p>}
    </div>
  );
}
