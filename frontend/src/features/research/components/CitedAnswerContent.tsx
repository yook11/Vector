"use client";

import type { JSX } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import type { ResearchAssistantMessage } from "@/types/types.gen";
import { useAnswerMarkdownConfig } from "../markdown/answer-markdown";
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

export function CitedAnswerContent({
  content,
  sources,
}: CitedAnswerContentProps) {
  const sourcesByRef = new Map(
    sources.map((source) => [source.sourceRef, source] as const),
  );
  const matchableRefs = new Set(sourcesByRef.keys());

  const {
    remarkPlugins,
    remarkRehypeOptions,
    components: sharedComponents,
  } = useAnswerMarkdownConfig();

  const components: Components = {
    ...sharedComponents,
    [CITATION_BADGE_TAG_NAME]: (props) =>
      CitationMarkerOrSpan(props, sourcesByRef),
  };

  return (
    <Markdown
      remarkPlugins={[
        ...remarkPlugins,
        [remarkCitationMarkers, { matchableRefs }],
      ]}
      remarkRehypeOptions={remarkRehypeOptions}
      components={components}
    >
      {content}
    </Markdown>
  );
}
