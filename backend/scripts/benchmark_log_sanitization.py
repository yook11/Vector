"""合成入力で旧検出結果との一致と情報漏洩防止の負荷を測り、JSONで標準出力へ記録する。"""

from __future__ import annotations

import json
import platform
import random
import re
import statistics
import time
from collections.abc import Callable
from itertools import product

import structlog

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.budget import EVENT_TEXT_LIMIT, MAX_ITEMS_PER_LOG_EVENT, TEXT_LIMIT
from app.log_policy.leak_prevention import (
    prevent_credential_leaks,
    redact_jwts,
    redact_url_userinfo,
)
from app.log_policy.logger import create_policy_logger
from app.log_policy.processor import LogPolicyProcessor

_REFERENCE_URL = re.compile(r"([a-z][a-z0-9+.\-]*)://[^@/\s]+@", re.IGNORECASE)
_REFERENCE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_BATCHES = 7
_ITERATIONS = 3


def _repeat(seed: str, length: int) -> str:
    return (seed * (length // len(seed) + 1))[:length]


def _measure(call: Callable[[], object], *, iterations: int = _ITERATIONS) -> dict:
    call()
    samples = []
    for _ in range(_BATCHES):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            call()
        samples.append((time.perf_counter_ns() - start) / iterations / 1_000_000)
    return {
        "median_ms": round(statistics.median(samples), 6),
        "max_batch_mean_ms": round(max(samples), 6),
    }


def _check_equivalence() -> dict[str, int]:
    """乱数seedを固定し、境界の組合せと合成文を旧正規表現の結果に照合する。"""
    prefixes = ["", "-", "123", "1.", "a", "_", " ", "日本語", "İ", "K"]
    schemes = ["https", "postgresql+asyncpg", "123", "", "İ", "ı", "ſ", "K"]
    authorities = ["user:synthetic@host", "user@host", "host", "@host", "user/a@host"]
    urls = [
        f"{prefix}{scheme}://{authority}/path"
        for prefix, scheme, authority in product(prefixes, schemes, authorities)
    ]
    segments = [
        "",
        "a",
        "eyJ",
        "eyJa",
        "eyJeyJ",
        "prefixeyJa",
        "eyJa=",
        "EYJa",
        "_-",
        "日本語",
    ]
    jwts = [".".join(parts) for parts in product(segments, repeat=4)]
    rng = random.Random(20260918)  # noqa: S311 — 合成入力の再現性のためseedを固定する。
    fragments = [
        "eyJ",
        "eyJa",
        ".",
        "a",
        "_-",
        "=",
        " ",
        "/",
        "-https://user:synthetic@host",
        "İ",
        "://",
        "@",
    ]
    generated = [
        "".join(rng.choices(fragments, k=rng.randint(1, 30))) for _ in range(20000)
    ]
    for value in urls + generated:
        if redact_url_userinfo(value) != _REFERENCE_URL.sub(
            r"\1://[redacted:url_userinfo]@", value
        ):
            raise AssertionError(f"URLの置換結果が変わった: {value!r}")
    for value in jwts + generated:
        if redact_jwts(value) != _REFERENCE_JWT.sub("[redacted:jwt]", value):
            raise AssertionError(f"JWTの置換結果が変わった: {value!r}")
    return {
        "url_cases": len(urls) + len(generated),
        "jwt_cases": len(jwts) + len(generated),
    }


def main() -> None:
    """現行の文字数制限は変更せず、情報漏洩防止単体とprocessor全体を測定する。"""
    report: dict = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "input_limits": {
            "text": TEXT_LIMIT,
            "event_text": EVENT_TEXT_LIMIT,
            "values": MAX_ITEMS_PER_LOG_EVENT,
        },
        "batches": _BATCHES,
        "iterations_per_batch": _ITERATIONS,
        "equivalence": _check_equivalence(),
        "strings": [],
        "legacy_comparison": [],
        "events": [],
    }
    for length in [500, 2048, 4096, 8192, 16384, 32768, 65536]:
        cases = {
            "ascii": "x" * length,
            "jwt_prefixes": _repeat("eyJ", length),
            "jwt_two_segments": _repeat("eyJ", length // 2)
            + "."
            + _repeat("eyJ", length - length // 2 - 1),
            "jwt_tokens": _repeat("eyJa.eyJb.c ", length),
            "public_url": "https://example.invalid/"
            + "x" * (length - len("https://example.invalid/")),
            "long_scheme": "x" * (length - len("://user:synthetic@host"))
            + "://user:synthetic@host",
            "assignments": _repeat("sample_key=synthetic ", length),
            "quoted_secret": 'password="' + _repeat(r"\"", length - 11) + '"',
            "japanese": _repeat("診断情報の確認。", length),
        }
        for name, value in cases.items():
            report["strings"].append(
                {
                    "case": name,
                    "chars": len(value),
                    **_measure(lambda: prevent_credential_leaks(value)),
                }
            )
    for length in [500, 2048, 10000]:
        for name, value, old, new in [
            (
                "url_ascii",
                "x" * length,
                lambda text: _REFERENCE_URL.sub(r"\1://[redacted:url_userinfo]@", text),
                redact_url_userinfo,
            ),
            (
                "jwt_prefixes",
                _repeat("eyJ", length),
                lambda text: _REFERENCE_JWT.sub("[redacted:jwt]", text),
                redact_jwts,
            ),
        ]:
            report["legacy_comparison"].append(
                {
                    "case": name,
                    "chars": len(value),
                    "before": _measure(lambda: old(value), iterations=1),
                    "after": _measure(lambda: new(value)),
                }
            )
    rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
    processor = LogPolicyProcessor()
    logger = create_policy_logger(
        "benchmark", rules, output_logger_factory=structlog.ReturnLoggerFactory()
    )
    for total in [8192, 16384, 32768, 65536]:
        for fields in [1, 16, 128, 512, 900]:
            for name, seed in [
                ("ascii", "x"),
                ("jwt_tokens", "eyJa.eyJb.c "),
                ("assignments", "sample_key=synthetic "),
            ]:
                keys = [f"field_{i}" for i in range(fields)]
                key_chars = sum(map(len, keys))
                value_length, remainder = divmod(
                    total - key_chars - len("benchmark"), fields
                )
                payload = {
                    key: _repeat(seed, value_length + (index < remainder))
                    for index, key in enumerate(keys)
                }
                event = {
                    "event": "benchmark",
                    "payload": payload,
                }
                prepared_event = processor(logger, "info", event)
                report["events"].append(
                    {
                        "case": name,
                        "fields": fields,
                        "input_chars_excluding_top_level_keys": total,
                        "budget_limit_reason": prepared_event.get(
                            "_policy_limit_reason"
                        ),
                        "payload_retained": "payload" in prepared_event,
                        "value_chars": sum(map(len, payload.values())),
                        "key_chars": key_chars,
                        **_measure(lambda: processor(logger, "info", event)),
                    }
                )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
