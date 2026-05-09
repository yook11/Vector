import type { Metadata } from "next";
import { NewsDetailBaseline } from "./_components/baseline";
import { NewsDetailOptionA } from "./_components/option-a";
import { NewsDetailOptionB } from "./_components/option-b";
import { NewsDetailOptionC } from "./_components/option-c";
import { OptionFrame } from "./_components/option-frame";

export const metadata: Metadata = {
  title: "Design Lab — NewsDetail | Vector",
  robots: { index: false, follow: false },
};

export default function NewsDetailDesignLabPage() {
  return (
    <main className="min-h-full">
      {/* Page intro */}
      <div className="mx-auto max-w-4xl px-4 py-14 sm:py-20">
        <p className="mb-3 text-[11px] font-medium uppercase tracking-[0.22em] text-muted-foreground">
          Design Lab
        </p>
        <h1 className="mb-4 text-3xl font-medium tracking-tight text-foreground sm:text-4xl">
          NewsDetail リデザイン比較
        </h1>
        <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground sm:text-base">
          AI Summary と Investor Take の <strong>役割の違い</strong>と
          <strong>ブロック境界</strong>を視覚化する 3 案。 現状実装 (Baseline)
          を最上段に置き、A → B → C の順で介入度が強くなる。
          記事データはスクリーンショットの Honker 記事を流用。
        </p>

        <div className="mt-8 grid gap-3 text-xs text-muted-foreground sm:grid-cols-3">
          <div className="rounded-md border border-border/60 px-4 py-3">
            <p className="mb-1 font-semibold text-foreground">A</p>
            <p>ラベル意味化 + 余白差。最小変更。</p>
          </div>
          <div className="rounded-md border border-border/60 px-4 py-3">
            <p className="mb-1 font-semibold text-foreground">B</p>
            <p>性質差をビジュアル化。引用ブロック。</p>
          </div>
          <div className="rounded-md border border-border/60 px-4 py-3">
            <p className="mb-1 font-semibold text-foreground">C</p>
            <p>領域分割。記事と編集を別文書として扱う。</p>
          </div>
        </div>
      </div>

      <OptionFrame
        tone="baseline"
        badge="Baseline"
        title="現状実装"
        intent="今の NewsDetail.tsx をデータ差替えのみで再現したもの。中央寄せ・同型ラベルが繰り返されており、AI Summary と Investor Take の役割差・境界が読み取りにくい状態を確認するための基準。"
        pros={[
          "実装はシンプルで、装飾もミニマル",
          "ブランドの瞑想的トーン (大きな余白 + 中央寄せ) は維持されている",
        ]}
        cons={[
          "AI Summary と Investor Take が同一フォーマットで並列、性質差が伝わらない",
          "ラベルが small uppercase 英語のみで、何のブロックか読まずに分からない",
          "ブロック内段落間隔とブロック間隔が同じリズムで、境界が曖昧",
        ]}
      >
        <NewsDetailBaseline />
      </OptionFrame>

      <OptionFrame
        badge="Option A"
        title="ラベル意味化 + 余白差"
        intent="ラベルを「記事の要約」「Vector の見立て」と日本語主タイトル + 1 行の意図サブ説明に拡張し、ブロック内は密 (space-y-3 / 5)、ブロック間は緩 (my-16 + border-t) にして Gestalt の近接の原理を効かせる。装飾は足さない最小介入。"
        pros={[
          "実装変更が最小 (構造とラベル文言のみ)、リスク低",
          "ラベルが「機能名」から「ブロックの役割」を伝える文に変わる",
          "余白の対比でブロック境界が直感的に分かる",
        ]}
        cons={[
          "AI Summary と Investor Take の見た目は依然として同型 (差は余白のみ)",
          "性質差 (客観 vs 主観) は読まないと伝わらない",
        ]}
      >
        <NewsDetailOptionA />
      </OptionFrame>

      <OptionFrame
        badge="Option B (採用)"
        title="性質差ビジュアル化 + 可読性チューニング"
        intent="A の構造に加え、Investor Take を引用ブロック扱い (left border + 薄背景 + Compass icon) に。さらに長文の「文字のかたまり」感を解消するため AI 出力を段落分割 + 行間 1.9 + 段落間 space-y-5 で呼吸させる。見出しは text-balance + clamp() で文字数に応じて柔軟にスケール、原題は italic + 左 rule で翻訳タイトルとの階層を視覚化。"
        pros={[
          "性質の違いが一目で伝わる (客観の地 vs 引用された編集)",
          "段落分割 + 行間広めで長文の塊感が消え、読み始めやすい",
          "見出しが画面幅・文字数に応じて柔軟にスケール (clamp + text-balance)",
          "原題の italic + 左 rule で翻訳タイトルとの階層が立つ",
        ]}
        cons={[
          "段落分割を効かせるには AI 出力側に paragraph break を含める改修が望ましい (現状は frontend 側で \\n\\n split)",
          "primary 色を装飾に使うのでアクセントがやや増える",
          "引用ブロックは「枠の一種」なので、ブランド方針と要相談",
        ]}
      >
        <NewsDetailOptionB />
      </OptionFrame>

      <OptionFrame
        badge="Option C"
        title="領域分割 — 記事と編集を別文書として扱う"
        intent="AI 要約は「記事本文」として label を消し、原文要約だと足元に小さく注記。その後 Vector Analysis のワードマーク + horizontal rule で領域転換を強く示し、Investor Take を独立した編集記事として h2 大見出し + 説明文付きで再開する。最も大胆な再構築案。"
        pros={[
          "「記事を読む」体験と「Vector の解釈を読む」体験が完全に分離される",
          "Investor Take が独立した編集アウトプットとしての存在感を持つ",
          "ブロック境界の曖昧さは構造的に消える (territory が違う)",
        ]}
        cons={[
          "短い記事だと領域 1 が薄く、間延びして見える可能性",
          "現状 UI からの心理的距離が大きい (慣れの再学習が必要)",
          "Vector Analysis ワードマークが効かない場合は装飾過剰に見える",
        ]}
      >
        <NewsDetailOptionC />
      </OptionFrame>

      <div className="mx-auto max-w-4xl border-t border-border/60 px-4 py-12">
        <p className="text-xs text-muted-foreground">
          このページは design-lab 用のモックです。本番 NewsDetail は
          <code className="mx-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
            src/features/news/components/NewsDetail.tsx
          </code>
          にあります。採用案決定後にそちらへ反映してください。
        </p>
      </div>
    </main>
  );
}
