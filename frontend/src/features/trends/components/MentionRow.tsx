"use client";

import { ChevronDown } from "lucide-react";
import { useState } from "react";
import type { RankedMention } from "@/types";
import { MENTION_TYPE_META } from "../display";
import { GrowthTag } from "./GrowthTag";
import { TypeBadge } from "./TypeBadge";

type ColumnMode = "count" | "growth";

interface MentionRowProps {
  rank: number;
  mention: RankedMention;
  mode: ColumnMode;
}

/** 固有名1行。クリックで展開(要点+共起)する client component。 */
export function MentionRow({ rank, mention, mode }: MentionRowProps) {
  const [expanded, setExpanded] = useState(false);
  const isNew = mention.previousAppearanceCount === 0;

  return (
    <li className="border-b border-[var(--vector-line)] last:border-b-0">
      {/* 行本体 */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2.5 py-2.5 px-0 text-left group"
        aria-expanded={expanded}
      >
        {/* 順位 */}
        <span
          className="shrink-0 w-5 text-[12px] tabular-nums text-[var(--vector-ink-muted)] italic leading-none"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {rank}
        </span>

        {/* 名前 + バッジ */}
        <div className="flex-1 flex flex-col gap-0.5 min-w-0">
          <span
            className="text-[14.5px] font-bold leading-tight text-[var(--vector-ink)] truncate"
            style={{ fontFamily: "var(--font-vector-serif)" }}
          >
            {mention.name}
          </span>
          <TypeBadge type={mention.type} />
        </div>

        {/* 指標ブロック */}
        <div className="shrink-0 flex flex-col items-end gap-0.5 ml-1">
          {mode === "count" ? (
            <>
              <span
                className="text-[15px] font-bold tabular-nums text-[var(--vector-ink)] leading-none"
                style={{ fontFamily: "var(--font-vector-display)" }}
              >
                {mention.appearanceCount}
                <span
                  className="text-[10px] font-normal ml-0.5 text-[var(--vector-ink-muted)]"
                  style={{ fontFamily: "var(--font-vector-maru)" }}
                >
                  件
                </span>
              </span>
              <div className="flex items-center gap-1.5">
                <GrowthTag
                  growthRate={mention.growthRate}
                  previousAppearanceCount={mention.previousAppearanceCount}
                />
                <span
                  className="text-[10px] text-[var(--vector-ink-muted)] tabular-nums"
                  style={{ fontFamily: "var(--font-vector-maru)" }}
                >
                  前週 {mention.previousAppearanceCount}
                </span>
              </div>
            </>
          ) : (
            <>
              <GrowthTag
                growthRate={mention.growthRate}
                previousAppearanceCount={mention.previousAppearanceCount}
              />
              <div className="flex items-center gap-1.5">
                {isNew && (
                  <span
                    className="inline-block rounded-[2px] bg-[var(--vector-accent-tint)] px-1 py-px text-[9.5px] font-semibold tracking-[0.06em] text-[var(--vector-accent-ink)]"
                    style={{ fontFamily: "var(--font-vector-maru)" }}
                  >
                    新登場
                  </span>
                )}
                <span
                  className="text-[10px] text-[var(--vector-ink-muted)] tabular-nums"
                  style={{ fontFamily: "var(--font-vector-maru)" }}
                >
                  {mention.previousAppearanceCount} → {mention.appearanceCount}
                  件
                </span>
              </div>
            </>
          )}
        </div>

        {/* chevron */}
        <ChevronDown
          aria-hidden="true"
          className="shrink-0 size-3.5 text-[var(--vector-ink-muted)] transition-transform duration-150"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }}
        />
      </button>

      {/* 展開パネル */}
      {expanded && <MentionDetail mention={mention} />}
    </li>
  );
}

function MentionDetail({ mention }: { mention: RankedMention }) {
  return (
    <div className="grid gap-4 pb-4 px-0 md:grid-cols-2 border-t border-[var(--vector-line)] pt-3 mt-0">
      {/* 左: 要点 */}
      <div>
        <p
          className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          要点 · 何を言われたか
        </p>
        {mention.keyPoints.length === 0 ? (
          <p
            className="text-[12.5px] text-[var(--vector-ink-muted)] italic"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            要点は登録されていません
          </p>
        ) : (
          <ul className="space-y-2">
            {mention.keyPoints.map((point, i) => (
              <li
                // biome-ignore lint/suspicious/noArrayIndexKey: 要点順序は AI 出力に従い安定
                key={i}
                className="flex gap-2 text-[13px] leading-[1.8] text-[var(--vector-ink-soft)]"
                style={{ fontFamily: "var(--font-vector-serif)" }}
              >
                <span
                  aria-hidden="true"
                  className="mt-[0.65em] size-1.5 shrink-0 rounded-full bg-[var(--vector-accent)]"
                />
                <span>{point}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 右: 共起 */}
      <div>
        <p
          className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          一緒に語られた
        </p>
        {mention.relatedMentions.length === 0 ? (
          <p
            className="text-[12.5px] text-[var(--vector-ink-muted)] italic"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            共起した固有名はありません
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {mention.relatedMentions.map((rel) => {
              const meta = MENTION_TYPE_META[rel.type];
              return (
                <span
                  key={`${rel.type}:${rel.name}`}
                  className="inline-flex items-center gap-1.5 rounded border border-[var(--vector-line)] px-2 py-1"
                >
                  <span
                    aria-hidden="true"
                    className="inline-block size-[6px] rounded-[1px] shrink-0"
                    style={{ backgroundColor: meta.color }}
                  />
                  <span
                    className="text-[12px] text-[var(--vector-ink-soft)]"
                    style={{ fontFamily: "var(--font-vector-serif)" }}
                  >
                    {rel.name}
                  </span>
                  <span
                    className="text-[10.5px] italic text-[var(--vector-ink-muted)] tabular-nums"
                    style={{ fontFamily: "var(--font-vector-display)" }}
                  >
                    {rel.sharedArticleCount}件
                  </span>
                </span>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
