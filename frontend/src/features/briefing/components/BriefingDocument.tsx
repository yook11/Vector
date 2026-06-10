import { ChevronLeft, Sparkles } from "lucide-react";
import Link from "next/link";
import { formatPaperDate, formatPaperTime } from "@/components/paper";
import type { BriefingResponseParsed } from "../schemas/briefing";
import { BriefingDisclaimer } from "./BriefingDisclaimer";
import { KeyArticleBlock } from "./KeyArticleBlock";
import { WatchPoints } from "./WatchPoints";

type BriefingDetail = Extract<BriefingResponseParsed, { state: "briefing" }>;

/** week_start (ISO date) から 7 日窓の終端 (start + 6 日) を ISO で返す。 */
function weekEndIso(weekStartIso: string): string {
  const end = new Date(weekStartIso);
  end.setUTCDate(end.getUTCDate() + 6);
  return end.toISOString();
}

function SectionHeading({
  eyebrow,
  title,
}: {
  eyebrow?: string;
  title: string;
}) {
  return (
    <div className="mb-9 text-center">
      {eyebrow && (
        <div
          className="mb-3 text-[12px] font-semibold uppercase tracking-[0.3em] text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {eyebrow}
        </div>
      )}
      <h2
        className="text-[clamp(22px,3vw,30px)] font-bold text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        {title}
      </h2>
      <div className="mx-auto mt-4 h-px w-16 bg-[var(--vector-ink)] opacity-50" />
    </div>
  );
}

/** 週次 briefing を雑誌風の紙面読み物として描画する (news 詳細の paper idiom を踏襲)。 */
export function BriefingDocument({ briefing }: { briefing: BriefingDetail }) {
  return (
    <article className="pt-7 pb-4">
      <div className="mb-8">
        <Link
          href="/briefing"
          className="inline-flex items-center gap-1.5 text-[12.5px] tracking-[0.04em] text-[var(--vector-ink-muted)] transition-colors hover:text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          <ChevronLeft aria-hidden="true" className="size-3.5" />
          一覧に戻る
        </Link>
      </div>

      {/* カバー: 中央寄せ。見出し → 週レンジ → 生成メタの階層を罫線と書体で示す。 */}
      <header className="mx-auto mb-12 max-w-[820px] text-center">
        <div className="mb-5 flex items-center justify-center gap-4">
          <span className="h-px w-[clamp(40px,10vw,120px)] bg-[var(--vector-line)]" />
          <span
            className="text-[13px] font-semibold uppercase tracking-[0.34em] text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            Weekly Briefing
          </span>
          <span className="h-px w-[clamp(40px,10vw,120px)] bg-[var(--vector-line)]" />
        </div>
        <p
          className="mb-5 text-[14px] italic text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {formatPaperDate(briefing.weekStart)} —{" "}
          {formatPaperDate(weekEndIso(briefing.weekStart))}
        </p>
        <h1
          className="text-balance text-[clamp(30px,4.4vw,52px)] font-extrabold leading-[1.22] text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {briefing.headline}
        </h1>
        <div className="mt-6 flex justify-center">
          <span
            className="inline-flex items-center gap-1.5 text-[13px] italic text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            <Sparkles
              aria-hidden="true"
              className="size-3.5 text-[var(--vector-accent)]"
            />
            AIが今週 {briefing.inputArticleCount} 件の記事から生成 ·{" "}
            {formatPaperDate(briefing.generatedAt)}{" "}
            {formatPaperTime(briefing.generatedAt)}
          </span>
        </div>
      </header>

      {/* リード: 今週の総括。中央寄せ・大きめ serif、先頭文字をアクセント。 */}
      <p
        className="mx-auto max-w-[34em] whitespace-pre-line text-pretty text-center text-[clamp(19px,2.1vw,24px)] leading-[1.9] text-[var(--vector-ink)] first-letter:pr-[0.04em] first-letter:text-[1.9em] first-letter:font-bold first-letter:leading-[0.9] first-letter:text-[var(--vector-accent-ink)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        {briefing.summary}
      </p>

      {/* 章タイムライン: 左スパイン + 菱形マーカーで章の連なりを示す。 */}
      <div className="relative mx-auto mt-14 max-w-[760px]">
        {briefing.chapters.map((chapter, i) => {
          const last = i === briefing.chapters.length - 1;
          return (
            <section
              key={chapter.heading}
              className={last ? "relative pl-10" : "relative pb-10 pl-10"}
            >
              {!last && (
                <span
                  aria-hidden="true"
                  className="absolute bottom-0 left-[5px] top-6 w-px bg-[var(--vector-rule)]"
                />
              )}
              <span
                aria-hidden="true"
                className="absolute left-0 top-[7px] size-3 rotate-45 bg-[var(--vector-accent)] shadow-[0_0_0_4px_var(--vector-paper)]"
              />
              <h2
                className="mb-3 text-[clamp(20px,2.3vw,24px)] font-bold leading-[1.32] text-[var(--vector-ink)]"
                style={{ fontFamily: "var(--font-vector-serif)" }}
              >
                {chapter.heading}
              </h2>
              <p
                className="whitespace-pre-line text-pretty text-[clamp(15.5px,1.8vw,17px)] leading-[2.0] text-[var(--vector-ink-soft)]"
                style={{ fontFamily: "var(--font-vector-serif)" }}
              >
                {chapter.body}
              </p>
            </section>
          );
        })}
      </div>

      {briefing.watchPoints.length > 0 && (
        <section className="mt-16">
          <SectionHeading eyebrow="Forecast" title="今後の注目点" />
          <WatchPoints watchPoints={briefing.watchPoints} />
        </section>
      )}

      {briefing.keyArticles.length > 0 && (
        <section className="mt-16">
          <SectionHeading title="特に重要な記事" />
          <div className="mx-auto flex max-w-[860px] flex-col">
            {briefing.keyArticles.map((keyArticle, i) => (
              <KeyArticleBlock
                key={keyArticle.article.id}
                keyArticle={keyArticle}
                category={briefing.category}
                index={i}
              />
            ))}
          </div>
        </section>
      )}

      <div className="mx-auto mt-14 max-w-[860px] border-t border-[var(--vector-line)] pt-6">
        <BriefingDisclaimer />
      </div>
    </article>
  );
}
