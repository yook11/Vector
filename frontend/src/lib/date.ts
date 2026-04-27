export function formatDate(
  dateStr: string | null | undefined,
  opts?: { withTime?: boolean },
): string {
  if (!dateStr) return "Unknown";
  const base: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: "long",
    day: "numeric",
  };
  if (opts?.withTime) {
    base.hour = "2-digit";
    base.minute = "2-digit";
  }
  return new Date(dateStr).toLocaleDateString("ja-JP", base);
}
