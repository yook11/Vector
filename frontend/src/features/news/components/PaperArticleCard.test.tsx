import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ArticleBrief } from "@/types/types.gen";
import { PaperArticleCard } from "./PaperArticleCard";

// types.gen.ts は未再生成で summary を持つ旧スキーマのまま。
// 新契約 (keyPoints / summaryPreview) を as unknown as ArticleBrief でキャストして
// ターゲット契約を先行エンコードする (Red-first)。
type ArticleBriefNew = Omit<ArticleBrief, "summary"> & {
  keyPoints: string[];
  summaryPreview: string | null;
};

function makeArticle(
  overrides: Partial<ArticleBriefNew> = {},
): ArticleBriefNew {
  return {
    id: 101,
    translatedTitle: "Claude Mythosが明らかにした遅延問題",
    keyPoints: [
      "AIによる脆弱性の悪用が加速している",
      "パッチ体制が追いついていない",
    ],
    summaryPreview: null,
    category: {
      name: "セキュリティ",
      slug: "security",
    },
    source: {
      attributionLabel: "Hacker News",
      name: "Hacker News",
    },
    publishedAt: "2026-05-31T02:30:00.000Z",
    ...overrides,
  };
}

describe("PaperArticleCard — 構造", () => {
  it("タイトルリンクが /news/{id} の href を持つ", () => {
    render(
      <PaperArticleCard
        article={makeArticle() as unknown as ArticleBrief}
        actionSlot={<button type="button">保存</button>}
      />,
    );

    expect(
      screen.getByRole("link", { name: "Claude Mythosが明らかにした遅延問題" }),
    ).toHaveAttribute("href", "/news/101");
    expect(screen.getByText("セキュリティ")).toBeInTheDocument();
    expect(screen.getByText("SECURITY")).toBeInTheDocument();
    expect(screen.getByText("Hacker News")).toBeInTheDocument();
    expect(screen.getByText("2026年5月31日")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
  });
});

describe("PaperArticleCard — keyPoints 表示分岐", () => {
  it("keyPoints が3件・summaryPreview: null のとき各 keyPoint テキストが表示される", () => {
    // invariant: keyPoints が非空なら各 content が document に存在する
    const points = [
      "AIによる脆弱性の悪用が加速している",
      "パッチ体制が追いついていない",
      "ゼロデイ公開から平均4.5日で攻撃が始まる",
    ];
    render(
      <PaperArticleCard
        article={
          makeArticle({
            keyPoints: points,
            summaryPreview: null,
          }) as unknown as ArticleBrief
        }
      />,
    );

    for (const point of points) {
      expect(screen.getByText(point)).toBeInTheDocument();
    }
    // key point list は accessible name "要点" を持つ(別 list と区別する anchor)。
    expect(screen.getByRole("list", { name: "要点" })).toBeInTheDocument();
  });

  it("keyPoints が非空なら keyPoint を表示し summaryPreview は表示しない", () => {
    // mutual-exclusion 表示分岐: keyPoints が非空のとき keyPoint を描画し summaryPreview
    // を描画しない。両側を同一 render で assert しないと、何も描画しない未実装/壊れた
    // 実装に対し discriminate できない (sentinel 不在のみでは自明に pass し空虚になる)。
    const keyPoint = "AIによる脆弱性の悪用が加速している";
    const sentinel = "SENTINEL_PREVIEW_TEXT_SHOULD_NOT_APPEAR";
    render(
      <PaperArticleCard
        article={
          makeArticle({
            keyPoints: [keyPoint],
            summaryPreview: sentinel,
          }) as unknown as ArticleBrief
        }
      />,
    );

    expect(screen.getByText(keyPoint)).toBeInTheDocument();
    expect(screen.queryByText(sentinel)).not.toBeInTheDocument();
  });

  it("keyPoints が空・summaryPreview に文字列があるとき summaryPreview が表示され keyPoint のリストは描画されない", () => {
    // invariant: keyPoints が空 → summaryPreview を表示し、リストを描画しない
    const preview =
      "AIによる脆弱性悪用のスピードが加速しており、企業のパッチ対応が追いついていない状況が続く。";
    render(
      <PaperArticleCard
        article={
          makeArticle({
            keyPoints: [],
            summaryPreview: preview,
          }) as unknown as ArticleBrief
        }
      />,
    );

    expect(screen.getByText(preview)).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "要点" }),
    ).not.toBeInTheDocument();
  });
});
