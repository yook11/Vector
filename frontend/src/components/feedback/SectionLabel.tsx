import { cn } from "@/lib/utils/cn";

type SectionLabelProps = {
  as?: "span" | "h2" | "h3";
  className?: string;
  children: React.ReactNode;
};

export function SectionLabel({
  as: Tag = "span",
  className,
  children,
}: SectionLabelProps) {
  return (
    <Tag
      className={cn(
        "text-xs uppercase tracking-widest text-muted-foreground",
        className,
      )}
    >
      {children}
    </Tag>
  );
}
