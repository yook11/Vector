import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  type AnswerMarkdownConfig,
  useAnswerMarkdownConfig,
} from "./answer-markdown";

const COMPONENT_KEYS = [
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "p",
  "ul",
  "ol",
  "blockquote",
  "pre",
  "code",
  "table",
  "th",
  "td",
  "a",
  "img",
] as const;

function MarkdownConfigProbe({
  onConfig,
}: {
  onConfig: (config: AnswerMarkdownConfig) => void;
}) {
  onConfig(useAnswerMarkdownConfig());
  return null;
}

describe("useAnswerMarkdownConfig", () => {
  it("同一mountのrerenderでplugin/options/componentsとfootnote namespaceのidentityを保つ", () => {
    let currentConfig: AnswerMarkdownConfig | undefined;
    const onConfig = (config: AnswerMarkdownConfig) => {
      currentConfig = config;
    };
    const view = render(<MarkdownConfigProbe onConfig={onConfig} />);
    const firstConfig = currentConfig;
    if (firstConfig === undefined) {
      throw new Error("initial Markdown config is missing");
    }

    view.rerender(<MarkdownConfigProbe onConfig={onConfig} />);
    const secondConfig = currentConfig;
    if (secondConfig === undefined) {
      throw new Error("rerendered Markdown config is missing");
    }

    expect(secondConfig.remarkPlugins).toBe(firstConfig.remarkPlugins);
    expect(secondConfig.remarkRehypeOptions).toBe(
      firstConfig.remarkRehypeOptions,
    );
    expect(secondConfig.remarkRehypeOptions.clobberPrefix).toBe(
      firstConfig.remarkRehypeOptions.clobberPrefix,
    );
    expect(secondConfig.components).toBe(firstConfig.components);
    for (const componentKey of COMPONENT_KEYS) {
      expect(secondConfig.components[componentKey]).toBe(
        firstConfig.components[componentKey],
      );
    }
  });
});
