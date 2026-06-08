import type { Metadata } from "next";
import { connection } from "next/server";
import { PaperSurface, PaperTexture } from "@/components/paper";
import {
  BriefingIndexView,
  getBriefingListViewModel,
} from "@/features/briefing";
import { requireSession } from "@/lib/auth/guards";

export const metadata: Metadata = { title: "Briefing | Vector" };

export default async function BriefingListPage() {
  await requireSession();
  await connection();
  const data = await getBriefingListViewModel();
  return (
    <PaperSurface>
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <BriefingIndexView data={data} />
      </div>
    </PaperSurface>
  );
}
