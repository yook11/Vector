"""Stage 4 assessor shadow run: Gemini と DeepSeek の比較レポートを生成する。

PR-A merge 後、本番切替 (PR-C) 前に本番影響なしで両 assessor を並列呼び出しし、
spec の判定基準を計測する:

- カテゴリ一致率: Gemini 比 ±5% 以内
- Pydantic 検証失敗率: < 1%
- per-call レイテンシ: Gemini 比 1.5× 以内

Sample は ``article_extractions`` の最新 N 件 (default 100)。両 assessor を
直接 import して並列呼び出しするため、production の adapter wiring
(brokers.py の composition root) には影響しない。本スクリプトは PR-B の検証
専用で、判定完了後 (cutover 完了時) に削除する想定。

Usage:
    docker compose exec backend python scripts/compare_assessors.py
    docker compose exec backend python scripts/compare_assessors.py --limit 25

Output:
    discussions/<YYYY-MM-DD>-stage2-shadow-result.md
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.assessment.ai.gemini import GeminiAssessor
from app.analysis.assessment.domain.result import (
    InScope,
    OutOfScope,
    ValidCategory,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.db import engine
from app.models.article_curation import ArticleCuration


@dataclass(frozen=True)
class Sample:
    """Stage 2 への 1 件分の入力。"""

    curation_id: int
    title_ja: str
    summary_ja: str


@dataclass(frozen=True)
class CallResult:
    """1 件分の assessor 呼び出し結果。"""

    category_value: str | None
    investor_take: str | None
    latency_seconds: float
    error_class: str | None
    error_message: str | None
    is_validation_error: bool


@dataclass(frozen=True)
class SampleResult:
    sample: Sample
    gemini: CallResult
    deepseek: CallResult


async def _call_assessor(assessor: BaseAssessor, sample: Sample) -> CallResult:
    """1 件を assessor に通し、レイテンシと結果/エラーを記録する。"""
    start = time.perf_counter()
    try:
        result = await assessor.assess(
            title_ja=sample.title_ja, summary_ja=sample.summary_ja
        )
    except (AIProviderError, AssessmentResponseInvalidError) as exc:
        elapsed = time.perf_counter() - start
        return CallResult(
            category_value=None,
            investor_take=None,
            latency_seconds=elapsed,
            error_class=type(exc).__name__,
            error_message=str(exc),
            is_validation_error=isinstance(exc, AssessmentResponseInvalidError),
        )
    elapsed = time.perf_counter() - start

    if isinstance(result, OutOfScope):
        return CallResult(
            category_value=ValidCategory.OUT_OF_SCOPE.value,
            investor_take=result.investor_take,
            latency_seconds=elapsed,
            error_class=None,
            error_message=None,
            is_validation_error=False,
        )
    assert isinstance(result, InScope)  # noqa: S101 — tagged union exhaustiveness
    return CallResult(
        category_value=result.category.value,
        investor_take=result.investor_take,
        latency_seconds=elapsed,
        error_class=None,
        error_message=None,
        is_validation_error=False,
    )


async def _process_sample(
    sample: Sample,
    gemini: GeminiAssessor,
    deepseek: DeepSeekAssessor,
) -> SampleResult:
    g_res, d_res = await asyncio.gather(
        _call_assessor(gemini, sample),
        _call_assessor(deepseek, sample),
    )
    return SampleResult(sample=sample, gemini=g_res, deepseek=d_res)


async def _load_samples(limit: int) -> list[Sample]:
    async with AsyncSession(engine) as session:
        stmt = (
            select(ArticleCuration)
            .order_by(ArticleCuration.extracted_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return [
        Sample(
            curation_id=row.id,
            title_ja=row.translated_title,
            summary_ja=row.summary,
        )
        for row in rows
    ]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    if f == c:
        return sorted_v[f]
    return sorted_v[f] * (c - k) + sorted_v[c] * (k - f)


def _format_latency(values: list[float]) -> str:
    if not values:
        return "n/a"
    return (
        f"mean={statistics.mean(values):.3f}s "
        f"p50={statistics.median(values):.3f}s "
        f"p95={_percentile(values, 0.95):.3f}s "
        f"max={max(values):.3f}s"
    )


def _aggregate(results: list[SampleResult]) -> dict[str, Any]:
    n = len(results)
    g_ok = [r for r in results if r.gemini.error_class is None]
    d_ok = [r for r in results if r.deepseek.error_class is None]
    both_ok = [
        r
        for r in results
        if r.gemini.error_class is None and r.deepseek.error_class is None
    ]
    matches = sum(
        1 for r in both_ok if r.gemini.category_value == r.deepseek.category_value
    )
    g_validation_fails = sum(1 for r in results if r.gemini.is_validation_error)
    d_validation_fails = sum(1 for r in results if r.deepseek.is_validation_error)
    g_latencies = [r.gemini.latency_seconds for r in g_ok]
    d_latencies = [r.deepseek.latency_seconds for r in d_ok]
    g_mean = statistics.mean(g_latencies) if g_latencies else 0.0
    d_mean = statistics.mean(d_latencies) if d_latencies else 0.0
    return {
        "n": n,
        "gemini_ok": len(g_ok),
        "deepseek_ok": len(d_ok),
        "both_ok": len(both_ok),
        "match": matches,
        "match_rate": matches / len(both_ok) if both_ok else 0.0,
        "gemini_validation_fails": g_validation_fails,
        "deepseek_validation_fails": d_validation_fails,
        "gemini_validation_fail_rate": g_validation_fails / n if n else 0.0,
        "deepseek_validation_fail_rate": d_validation_fails / n if n else 0.0,
        "gemini_latency": g_latencies,
        "deepseek_latency": d_latencies,
        "latency_ratio": d_mean / g_mean if g_mean > 0 else float("inf"),
    }


def _escape_cell(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|").strip()


def _render_markdown(
    results: list[SampleResult],
    stats: dict[str, Any],
    gemini_model: str,
    deepseek_model: str,
) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines: list[str] = [
        f"# Stage 4 assessor shadow run — {today}",
        "",
        f"- Gemini: `{gemini_model}`",
        f"- DeepSeek: `{deepseek_model}`",
        f"- Samples: {stats['n']} (extracted_at desc)",
        "",
        "## サマリ",
        "",
        "| 指標 | 値 | 判定基準 |",
        "|---|---|---|",
    ]

    match_pct = stats["match_rate"] * 100
    match_status = "PASS" if abs(100 - match_pct) <= 5 else "FAIL"
    lines.append(
        f"| カテゴリ一致率 | {match_pct:.1f}% "
        f"({stats['match']}/{stats['both_ok']}) | "
        f"Gemini 比 ±5% 以内 — {match_status} |"
    )

    g_fail_pct = stats["gemini_validation_fail_rate"] * 100
    d_fail_pct = stats["deepseek_validation_fail_rate"] * 100
    g_status = "PASS" if g_fail_pct < 1 else "FAIL"
    d_status = "PASS" if d_fail_pct < 1 else "FAIL"
    lines.append(
        f"| Pydantic 検証失敗率 (Gemini) | {g_fail_pct:.1f}% "
        f"({stats['gemini_validation_fails']}/{stats['n']}) | < 1% — {g_status} |"
    )
    lines.append(
        f"| Pydantic 検証失敗率 (DeepSeek) | {d_fail_pct:.1f}% "
        f"({stats['deepseek_validation_fails']}/{stats['n']}) | < 1% — {d_status} |"
    )

    latency_status = "PASS" if stats["latency_ratio"] <= 1.5 else "FAIL"
    lines.append(
        f"| レイテンシ比 (DeepSeek mean / Gemini mean) | "
        f"{stats['latency_ratio']:.2f}x | <= 1.5x — {latency_status} |"
    )
    lines += [
        "",
        f"- Gemini 成功: {stats['gemini_ok']}/{stats['n']}",
        f"- DeepSeek 成功: {stats['deepseek_ok']}/{stats['n']}",
        f"- 両方成功: {stats['both_ok']}/{stats['n']}",
        "",
        "## レイテンシ詳細",
        "",
        f"- Gemini: {_format_latency(stats['gemini_latency'])}",
        f"- DeepSeek: {_format_latency(stats['deepseek_latency'])}",
        "",
    ]

    mismatches = [
        r
        for r in results
        if r.gemini.error_class is None
        and r.deepseek.error_class is None
        and r.gemini.category_value != r.deepseek.category_value
    ]
    lines += [f"## カテゴリ不一致 ({len(mismatches)} 件)", ""]
    if mismatches:
        lines += [
            "| curation_id | Gemini | DeepSeek | title |",
            "|---|---|---|---|",
        ]
        for r in mismatches:
            lines.append(
                f"| {r.sample.curation_id} | "
                f"{r.gemini.category_value} | {r.deepseek.category_value} | "
                f"{_escape_cell(r.sample.title_ja[:80])} |"
            )
    else:
        lines.append("(なし)")
    lines.append("")

    errors = [
        r
        for r in results
        if r.gemini.error_class is not None or r.deepseek.error_class is not None
    ]
    lines += [f"## エラー ({len(errors)} 件)", ""]
    if errors:
        lines += [
            "| curation_id | provider | error_class | message |",
            "|---|---|---|---|",
        ]
        for r in errors:
            for provider, call in (("gemini", r.gemini), ("deepseek", r.deepseek)):
                if call.error_class is not None:
                    msg = _escape_cell((call.error_message or "")[:120])
                    lines.append(
                        f"| {r.sample.curation_id} | {provider} | "
                        f"{call.error_class} | {msg} |"
                    )
    else:
        lines.append("(なし)")
    lines.append("")

    return "\n".join(lines)


async def _run(limit: int, output_path: Path) -> int:
    samples = await _load_samples(limit)
    if not samples:
        print("No samples found in article_extractions", file=sys.stderr)
        return 1

    print(f"Loaded {len(samples)} samples")

    gemini = GeminiAssessor()
    deepseek = DeepSeekAssessor()

    results: list[SampleResult] = []
    for i, sample in enumerate(samples, 1):
        print(
            f"[{i}/{len(samples)}] curation_id={sample.curation_id}",
            end=" ",
            flush=True,
        )
        result = await _process_sample(sample, gemini, deepseek)
        g_label = result.gemini.category_value or f"ERR({result.gemini.error_class})"
        d_label = (
            result.deepseek.category_value or f"ERR({result.deepseek.error_class})"
        )
        match = "match" if g_label == d_label else "diff"
        print(f"G={g_label} D={d_label} {match}")
        results.append(result)

    stats = _aggregate(results)
    markdown = _render_markdown(results, stats, gemini.model_name, deepseek.model_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote report to {output_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=100, help="サンプル数 (default: 100)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "出力 markdown パス "
            "(default: discussions/<YYYY-MM-DD>-stage2-shadow-result.md)"
        ),
    )
    args = parser.parse_args()

    if args.output is None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        output_path = (
            Path(__file__).resolve().parent.parent.parent
            / "discussions"
            / f"{today}-stage2-shadow-result.md"
        )
    else:
        output_path = args.output

    return asyncio.run(_run(args.limit, output_path))


if __name__ == "__main__":
    sys.exit(main())
