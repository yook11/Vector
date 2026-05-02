/**
 * 投資助言 disclaimer。詳細ページフッターに 1 行で配置。
 *
 * AI 生成の briefing は教育・情報提供目的であり、特定銘柄の売買勧誘では
 * ないことを明示する。日本の金融商品取引法 (投資助言・代理業) との切り
 * 分けを意図した文言。
 */
export function BriefingDisclaimer() {
  return (
    <p className="text-[10px] text-muted-foreground border-t border-border/60 pt-4">
      本ページの内容は AI が公開ニュースから自動生成した要約・解説であり、
      投資助言・推奨ではありません。投資判断は読者ご自身の責任で行ってください。
    </p>
  );
}
