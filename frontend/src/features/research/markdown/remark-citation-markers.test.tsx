import { render } from "@testing-library/react";
import type { JSX } from "react";
import Markdown, { type Components, type ExtraProps } from "react-markdown";
import { describe, expect, it } from "vitest";
import { useAnswerMarkdownConfig } from "./answer-markdown";
import { CITATION_MARKER_CASES } from "./citation-marker-corpus";
import {
  CITATION_BADGE_REF_ATTRIBUTE,
  CITATION_BADGE_TAG_NAME,
  remarkCitationMarkers,
} from "./remark-citation-markers";

/**
 * citationBadge をテキストを持たない probe 要素 (`<i data-citation-ref>`) に差し替える。
 * mdast の citationBadge ノードを直接読む代わりに、DOM 上の probe 要素の有無・属性・
 * 出現順だけで「ref 列」と「除去後本文」の 2 契約を読み取れるようにするための代替経路。
 */
function CitationRefProbe(props: JSX.IntrinsicElements["span"] & ExtraProps) {
  const { node, ...rest } = props;
  const ref = node?.properties[CITATION_BADGE_REF_ATTRIBUTE];
  return typeof ref === "string" ? (
    <i data-citation-ref={ref} />
  ) : (
    <span {...rest} />
  );
}

interface ProbeMarkdownProps {
  content: string;
  matchableRefs: ReadonlySet<string>;
}

/** production の描画経路 (`useAnswerMarkdownConfig` + `CitedAnswerContent`) と同じ remark 構成に probe を差し込む。 */
function ProbeMarkdown({ content, matchableRefs }: ProbeMarkdownProps) {
  const { remarkPlugins, remarkRehypeOptions, components } =
    useAnswerMarkdownConfig();

  const probeComponents: Components = {
    ...components,
    [CITATION_BADGE_TAG_NAME]: CitationRefProbe,
  };

  return (
    <Markdown
      remarkPlugins={[
        ...remarkPlugins,
        [remarkCitationMarkers, { matchableRefs }],
      ]}
      remarkRehypeOptions={remarkRehypeOptions}
      components={probeComponents}
    >
      {content}
    </Markdown>
  );
}

/** DOM 順の `[data-citation-ref]` 要素から ref 列を初出順・重複排除で取り出す。 */
function citationRefsInOrder(container: HTMLElement): string[] {
  const refs = Array.from(
    container.querySelectorAll<HTMLElement>("[data-citation-ref]"),
  ).map((element) => element.dataset.citationRef ?? "");
  return [...new Set(refs)];
}

describe("remarkCitationMarkers — shared corpus (backend/frontend 契約の一致)", () => {
  it.each(
    CITATION_MARKER_CASES,
  )("$label: refs と除去後本文が仕様どおりになる", (testCase) => {
    const { container } = render(
      <ProbeMarkdown
        content={testCase.text}
        matchableRefs={new Set(testCase.refs)}
      />,
    );

    expect(citationRefsInOrder(container)).toEqual(testCase.refs);
    expect(container.textContent).toBe(testCase.stripped);
  });
});

describe("remarkCitationMarkers — グループ形の追加条件 (コーパス外、仕様 §7)", () => {
  it("グループ内の一部だけ matchableRefs にある場合、一致分だけがバッジになり非一致分は文字も残らない", () => {
    const { container } = render(
      <ProbeMarkdown content="前[[1], [9]]後" matchableRefs={new Set(["1"])} />,
    );

    expect(citationRefsInOrder(container)).toEqual(["1"]);
    expect(container.textContent).toBe("前後");
  });

  it("グループ内の ref が1件も matchableRefs に無い場合、グループ全体が除去される", () => {
    const { container } = render(
      <ProbeMarkdown content="前[[3], [4]]後" matchableRefs={new Set(["9"])} />,
    );

    expect(citationRefsInOrder(container)).toEqual([]);
    expect(container.textContent).toBe("前後");
  });

  it("link の内側にあるグループは matchable でも全て除去される", () => {
    const { container } = render(
      <ProbeMarkdown
        content="[text [[1], [2]] more](https://example.com)"
        matchableRefs={new Set(["1", "2"])}
      />,
    );

    expect(citationRefsInOrder(container)).toEqual([]);
    expect(container.textContent).not.toMatch(/\[\[\d/);
    expect(container.textContent).toContain("text");
    expect(container.textContent).toContain("more");
  });

  it("inline code 内のグループは置換されず literal のまま残る", () => {
    const { container } = render(
      <ProbeMarkdown
        content="前`[[1], [2]]`後"
        matchableRefs={new Set(["1", "2"])}
      />,
    );

    expect(citationRefsInOrder(container)).toEqual([]);
    const inlineCode = Array.from(container.querySelectorAll("code")).find(
      (element) => element.parentElement?.tagName !== "PRE",
    );
    expect(inlineCode?.textContent).toBe("[[1], [2]]");
  });

  it("fenced code 内のグループは置換されず literal のまま残る", () => {
    const { container } = render(
      <ProbeMarkdown
        content={"前\n\n```\n[[1], [2]]\n```\n"}
        matchableRefs={new Set(["1", "2"])}
      />,
    );

    expect(citationRefsInOrder(container)).toEqual([]);
    expect(container.querySelector("pre code")?.textContent?.trim()).toBe(
      "[[1], [2]]",
    );
  });
});

/**
 * DOM 順の `[data-citation-ref]` 要素を重複排除せずそのまま返す。
 * `citationRefsInOrder` は本文全体で重複排除するため、1 マッチ内の重複排除件数の
 * 固定には使えない (使うと空虚になる)。
 */
function citationRefsRaw(container: HTMLElement): string[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>("[data-citation-ref]"),
  ).map((element) => element.dataset.citationRef ?? "");
}

describe("remarkCitationMarkers — 1マッチ内の重複排除粒度 (コーパスでは固定不能, 仕様 §Tests)", () => {
  it("同一グループ内の重複 ref (`[[1], [1]]`) はバッジ1個になる", () => {
    const { container } = render(
      <ProbeMarkdown content="前[[1], [1]]後" matchableRefs={new Set(["1"])} />,
    );

    expect(citationRefsRaw(container)).toEqual(["1"]);
  });

  it("別々の位置にある同一 ref の marker (`[[1]]`...`[[1]]`) は重複排除されずバッジ2個になる", () => {
    const { container } = render(
      <ProbeMarkdown
        content="前[[1]]中[[1]]後"
        matchableRefs={new Set(["1"])}
      />,
    );

    expect(citationRefsRaw(container)).toEqual(["1", "1"]);
  });
});
