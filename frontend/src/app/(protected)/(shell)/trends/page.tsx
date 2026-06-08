import type { Metadata } from "next";
import { connection } from "next/server";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  getTrendsViewModel,
  TrendsEmptyState,
  TrendsView,
} from "@/features/trends";
import { requireSession } from "@/lib/auth/guards";

export const metadata: Metadata = {
  title: "トレンド | Vector",
};

export default async function TrendsPage() {
  // DAL gate: layout の認可は PPR の別 prerender 単位を守らないため、データ
  // 取得の前にここで認可して static shell 漏洩を塞ぐ。
  await requireSession();
  // build-time prerender を opt out して runtime fill に倒す。
  await connection();
  const data = await getTrendsViewModel();

  if (data.state === "empty") {
    return (
      <PaperSurface>
        <div className="relative min-h-dvh w-full overflow-hidden">
          <PaperTexture />
          <TrendsEmptyState />
        </div>
      </PaperSurface>
    );
  }

  return (
    <PaperSurface>
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <TrendsView data={data} />
      </div>
    </PaperSurface>
  );
}
