/** トレンド未生成時の空状態表示。紙面トーンに合わせた静かな表示。 */
export function TrendsEmptyState() {
  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center py-24 gap-2"
    >
      <p
        className="text-[16px] font-bold text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        該当するワードはありません
      </p>
      <p
        className="text-[12.5px] italic text-[var(--vector-ink-muted)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        次回の自動生成は JST 毎日 00:05 に予定されています
      </p>
    </div>
  );
}
