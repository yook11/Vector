"use client";

import type { JSX } from "react";
import { createElement, useId } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import type { ResearchAssistantMessage } from "@/types/types.gen";
import {
  CITATION_BADGE_REF_ATTRIBUTE,
  CITATION_BADGE_TAG_NAME,
  remarkCitationMarkers,
} from "../markdown/remark-citation-markers";
import { SourcePreviewBadge } from "./SourcePreviewBadge";

type ResearchCitationSource = ResearchAssistantMessage["sources"][number];

interface CitedAnswerContentProps {
  content: string;
  sources: ResearchCitationSource[];
}

type SourceHeadingTag = "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
type ShiftedHeadingTag = "h3" | "h4" | "h5" | "h6";

/** ページ階層 (thread タイトル = h2) より控えめな回答ローカルの意味レベルへ丸めるシフト・クランプ表。 */
const HEADING_LEVEL_SHIFT: Record<SourceHeadingTag, ShiftedHeadingTag> = {
  h1: "h3",
  h2: "h4",
  h3: "h5",
  h4: "h6",
  h5: "h6",
  h6: "h6",
};

/** シフト後タグごとの控えめな視覚スケール (h3〜h6 の視覚差を最大3段階程度に収める)。 */
const SHIFTED_HEADING_CLASS_NAME: Record<ShiftedHeadingTag, string> = {
  h3: "mt-5 mb-2 text-base font-semibold text-[var(--vector-ink)]",
  h4: "mt-4 mb-1.5 text-sm font-semibold text-[var(--vector-ink)]",
  h5: "mt-3 mb-1 text-sm font-semibold text-[var(--vector-ink)]",
  h6: "mt-3 mb-1 text-sm font-medium text-[var(--vector-ink-muted)]",
};

/**
 * remark-rehype が footnote label 見出しに固定で振る id。
 * clobberPrefix による名前空間化が及ばないため、この id だけ個別に書き換える。
 */
const FOOTNOTE_LABEL_ID = "footnote-label";

/** citation plugin が付けた data 属性の有無で、通常の `span` と引用バッジを判別する。 */
function CitationMarkerOrSpan(
  props: JSX.IntrinsicElements["span"] & ExtraProps,
  sourcesByRef: Map<string, ResearchCitationSource>,
) {
  const { node, ...rest } = props;
  const ref = node?.properties[CITATION_BADGE_REF_ATTRIBUTE];
  const source = typeof ref === "string" ? sourcesByRef.get(ref) : undefined;
  return source ? <SourcePreviewBadge source={source} /> : <span {...rest} />;
}

/** 見出しの意味レベルをシフト・クランプし、footnote label の id だけ回答単位で名前空間化する。 */
function MarkdownHeading(
  props: JSX.IntrinsicElements["h1"] & ExtraProps,
  originalTag: SourceHeadingTag,
  footnoteLabelId: string,
) {
  const { node: _node, id, ...rest } = props;
  const tag = HEADING_LEVEL_SHIFT[originalTag];
  return createElement(tag, {
    id: id === FOOTNOTE_LABEL_ID ? footnoteLabelId : id,
    className: SHIFTED_HEADING_CLASS_NAME[tag],
    ...rest,
  });
}

/**
 * fnref の aria-describedby は "footnote-label" 固定文字列を参照するため、
 * 名前空間化した label id に揃えて宙に浮いた参照を防ぐ。
 */
function namespaceFootnoteDescribedBy(
  describedBy: string | undefined,
  footnoteLabelId: string,
): string | undefined {
  if (!describedBy) {
    return describedBy;
  }
  return describedBy
    .split(/\s+/)
    .map((token) => (token === FOOTNOTE_LABEL_ID ? footnoteLabelId : token))
    .join(" ");
}

/**
 * 回答内 Markdown link / autolink は SourcePreviewBadge の外部リンクと同じ規約で新規タブに開く。
 * footnote の fnref / back-reference は同一ページ内の `#` fragment リンクであり、この規約の対象外。
 */
function MarkdownLink(
  props: JSX.IntrinsicElements["a"] & ExtraProps,
  footnoteLabelId: string,
) {
  const { node, "aria-describedby": ariaDescribedBy, href, ...rest } = props;
  const isPageInternalFragment = href?.startsWith("#") ?? false;
  return (
    <a
      {...rest}
      href={href}
      aria-describedby={namespaceFootnoteDescribedBy(
        ariaDescribedBy,
        footnoteLabelId,
      )}
      target={isPageInternalFragment ? undefined : "_blank"}
      rel={isPageInternalFragment ? undefined : "noreferrer"}
    />
  );
}

/** Markdown 画像は外部取得を発生させる `img` を描画せず、alt テキストのみ可視表示する。 */
function MarkdownImageAlt(props: JSX.IntrinsicElements["img"] & ExtraProps) {
  const { alt } = props;
  return <span>{alt}</span>;
}

/** テーブルを横スクロールコンテナへ収め、回答パネル自体の横スクロールを避ける。 */
function MarkdownTable(props: JSX.IntrinsicElements["table"] & ExtraProps) {
  const { node, ...rest } = props;
  return (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-left" {...rest} />
    </div>
  );
}

export function CitedAnswerContent({
  content,
  sources,
}: CitedAnswerContentProps) {
  const sourcesByRef = new Map(
    sources.map((source) => [source.sourceRef, source] as const),
  );
  const matchableRefs = new Set(sourcesByRef.keys());

  // remark-rehype の clobberPrefix を回答インスタンスごとに一意化し、footnote id の DOM 衝突を防ぐ (Invariant 6)。
  const instanceId = useId().replace(/[^a-zA-Z0-9-]/g, "");
  const clobberPrefix = `user-content-${instanceId}-`;
  const footnoteLabelId = `${clobberPrefix}footnote-label`;

  const components: Components = {
    [CITATION_BADGE_TAG_NAME]: (props) =>
      CitationMarkerOrSpan(props, sourcesByRef),
    h1: (props) => MarkdownHeading(props, "h1", footnoteLabelId),
    h2: (props) => MarkdownHeading(props, "h2", footnoteLabelId),
    h3: (props) => MarkdownHeading(props, "h3", footnoteLabelId),
    h4: (props) => MarkdownHeading(props, "h4", footnoteLabelId),
    h5: (props) => MarkdownHeading(props, "h5", footnoteLabelId),
    h6: (props) => MarkdownHeading(props, "h6", footnoteLabelId),
    p: (props) => {
      const { node, ...rest } = props;
      return <p className="my-2 first:mt-0 last:mb-0" {...rest} />;
    },
    ul: (props) => {
      const { node, ...rest } = props;
      return (
        <ul
          className="my-2 list-disc space-y-1 pl-5 first:mt-0 last:mb-0"
          {...rest}
        />
      );
    },
    ol: (props) => {
      const { node, ...rest } = props;
      return (
        <ol
          className="my-2 list-decimal space-y-1 pl-5 first:mt-0 last:mb-0"
          {...rest}
        />
      );
    },
    blockquote: (props) => {
      const { node, ...rest } = props;
      return (
        <blockquote
          className="my-2 border-l-2 border-[var(--vector-rule)] pl-3 text-[var(--vector-ink-muted)] first:mt-0 last:mb-0"
          {...rest}
        />
      );
    },
    pre: (props) => {
      const { node, ...rest } = props;
      return (
        <pre
          className="my-2 overflow-x-auto rounded-md bg-[var(--vector-surface)] p-3 font-mono text-xs leading-6 text-[var(--vector-ink)]"
          {...rest}
        />
      );
    },
    code: (props) => {
      const { node, ...rest } = props;
      return <code className="font-mono text-[0.85em]" {...rest} />;
    },
    table: MarkdownTable,
    th: (props) => {
      const { node, ...rest } = props;
      return (
        <th
          className="border-b border-[var(--vector-rule)] px-2 py-1 font-semibold text-[var(--vector-ink)]"
          {...rest}
        />
      );
    },
    td: (props) => {
      const { node, ...rest } = props;
      return (
        <td
          className="border-b border-[var(--vector-rule)] px-2 py-1 align-top"
          {...rest}
        />
      );
    },
    a: (props) => MarkdownLink(props, footnoteLabelId),
    img: MarkdownImageAlt,
  };

  return (
    <Markdown
      remarkPlugins={[
        remarkGfm,
        remarkBreaks,
        [remarkCitationMarkers, { matchableRefs }],
      ]}
      remarkRehypeOptions={{ clobberPrefix }}
      components={components}
    >
      {content}
    </Markdown>
  );
}
