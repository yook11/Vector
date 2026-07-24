import { readdirSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

type SourceSpan = {
  start: number;
  end: number;
};

type AnimationOccurrence = {
  animation: string;
  file: string;
  line: number;
  protectedByReducedMotion: boolean;
};

const SOURCE_DIRECTORY = resolve(process.cwd(), "src");
const GLOBAL_CSS_FILE = resolve(SOURCE_DIRECTORY, "app/globals.css");
const GENERATED_UI_PREFIX = "src/components/ui/";
const ANIMATION_PATTERN = /(?<![\w-])animate-(?:spin|pulse)(?![\w-])/g;
const REDUCED_MOTION_PATTERN =
  /motion-reduce:(?:animate-none|\[animation:none\])/;
const REDUCED_MOTION_MEDIA_PATTERN =
  /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{/g;
const ANIMATION_NONE_PATTERN =
  /\banimation\s*:\s*none(?:\s*!important)?\s*(?:;|$)/;

function productionTsxFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return productionTsxFiles(path);
    if (
      !entry.isFile() ||
      !entry.name.endsWith(".tsx") ||
      /\.(?:test|spec)\.tsx$/.test(entry.name)
    ) {
      return [];
    }
    return [path];
  });
}

function quotedSpanEnd(source: string, start: number): number {
  const quote = source[start];
  if (quote !== '"' && quote !== "'" && quote !== "`") return start + 1;

  for (let cursor = start + 1; cursor < source.length; cursor += 1) {
    if (source[cursor] === "\\") {
      cursor += 1;
      continue;
    }
    if (source[cursor] === quote) return cursor + 1;
  }
  return source.length;
}

function quotedSpans(source: string): SourceSpan[] {
  const spans: SourceSpan[] = [];
  for (let cursor = 0; cursor < source.length; cursor += 1) {
    const token = source[cursor];
    if (token !== '"' && token !== "'" && token !== "`") continue;
    const end = quotedSpanEnd(source, cursor);
    spans.push({ start: cursor, end });
    cursor = end - 1;
  }
  return spans;
}

function lineAt(source: string, index: number): number {
  return source.slice(0, index).split("\n").length;
}

function blockEnd(source: string, openingBrace: number): number {
  let depth = 0;
  for (let cursor = openingBrace; cursor < source.length; cursor += 1) {
    if (source[cursor] === "{") depth += 1;
    if (source[cursor] === "}") {
      depth -= 1;
      if (depth === 0) return cursor;
    }
  }
  return source.length;
}

function reducedMotionMediaBlocks(source: string): string[] {
  const blocks: string[] = [];
  let matched = REDUCED_MOTION_MEDIA_PATTERN.exec(source);
  while (matched !== null) {
    const openingBrace = matched.index + matched[0].length - 1;
    const closingBrace = blockEnd(source, openingBrace);
    blocks.push(source.slice(openingBrace + 1, closingBrace));
    matched = REDUCED_MOTION_MEDIA_PATTERN.exec(source);
  }
  return blocks;
}

function ruleSetsAnimationToNone(block: string, selector: string): boolean {
  let ruleStart = 0;
  for (let cursor = 0; cursor < block.length; cursor += 1) {
    if (block[cursor] !== "{") continue;
    const closingBrace = blockEnd(block, cursor);
    const selectors = block.slice(ruleStart, cursor);
    const declarations = block.slice(cursor + 1, closingBrace);
    if (
      selectors.includes(selector) &&
      ANIMATION_NONE_PATTERN.test(declarations)
    ) {
      return true;
    }
    ruleStart = closingBrace + 1;
    cursor = closingBrace;
  }
  return false;
}

function globalsReducedMotionViolations(): string[] {
  const globalCss = readFileSync(GLOBAL_CSS_FILE, "utf8");
  const mediaBlocks = reducedMotionMediaBlocks(globalCss);
  const selectors = [".animate-spin", ".animate-pulse"];

  return selectors.flatMap((selector) =>
    mediaBlocks.some((block) => ruleSetsAnimationToNone(block, selector))
      ? []
      : [
          `src/app/globals.css: ${selector} must set animation: none inside @media (prefers-reduced-motion: reduce)`,
        ],
  );
}

function isGeneratedUiFile(file: string): boolean {
  return relative(process.cwd(), file)
    .replaceAll("\\", "/")
    .startsWith(GENERATED_UI_PREFIX);
}

function scanAnimations(file: string): AnimationOccurrence[] {
  const source = readFileSync(file, "utf8");
  const strings = quotedSpans(source);
  const relativePath = relative(process.cwd(), file);
  const occurrences: AnimationOccurrence[] = [];
  let matched = ANIMATION_PATTERN.exec(source);

  while (matched !== null) {
    const index = matched.index;
    const string = strings.find(
      (span) => span.start <= index && index < span.end,
    );
    const owner = string;
    const ownerSource =
      owner === undefined
        ? source.slice(index, index + matched[0].length)
        : source.slice(owner.start, owner.end);

    occurrences.push({
      animation: matched[0],
      file: relativePath,
      line: lineAt(source, index),
      protectedByReducedMotion: REDUCED_MOTION_PATTERN.test(ownerSource),
    });
    matched = ANIMATION_PATTERN.exec(source);
  }
  return occurrences;
}

describe("production animation reduced-motion contract", () => {
  it("stops every spin and pulse source under prefers-reduced-motion", () => {
    const occurrences = productionTsxFiles(SOURCE_DIRECTORY)
      .filter((file) => !isGeneratedUiFile(file))
      .flatMap(scanAnimations);
    const handwrittenViolations = occurrences.filter(
      (occurrence) => !occurrence.protectedByReducedMotion,
    );
    const globalViolations = globalsReducedMotionViolations();

    expect(occurrences.length).toBeGreaterThan(0);
    if (globalViolations.length > 0 || handwrittenViolations.length > 0) {
      throw new Error(
        [
          ...globalViolations,
          "handwritten animate-spin / animate-pulse requires a local reduced-motion override",
          ...handwrittenViolations.map(
            (violation) =>
              `${violation.file}:${String(violation.line)} ${violation.animation}`,
          ),
        ].join("\n"),
      );
    }
  });
});
