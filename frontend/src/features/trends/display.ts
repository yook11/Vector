import type { MentionType } from "@/types";

// ---------------------------------------------------------------------------
// 種別表示辞書
// ---------------------------------------------------------------------------

export const MENTION_TYPE_META: Record<
  MentionType,
  { label: string; color: string }
> = {
  company: { label: "企業", color: "#B0852A" },
  product: { label: "製品", color: "#7A5BA8" },
  technology: { label: "技術", color: "#0E9E97" },
  person: { label: "人物", color: "#C04D6E" },
  academic: { label: "研究", color: "#3F84C0" },
  government: { label: "政府", color: "#5B6AB0" },
};

// ---------------------------------------------------------------------------
// カテゴリ表示辞書
// ---------------------------------------------------------------------------

interface CategoryDisplay {
  code: string;
  color: string;
}

const CATEGORY_DISPLAY: Record<string, CategoryDisplay> = {
  ai: { code: "A.I.", color: "#0E9E97" },
  computing: { code: "COMPUTE", color: "#7A5BA8" },
  semiconductor: { code: "SEMICON", color: "#C04D6E" },
};

export function getCategoryDisplay(slug: string): CategoryDisplay {
  return (
    CATEGORY_DISPLAY[slug] ?? { code: slug.toUpperCase(), color: "#B0852A" }
  );
}
