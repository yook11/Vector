"""新経路のHTML抽出と、抽出器との失敗境界。"""

import asyncio
from unittest.mock import patch

import pytest
from trafilatura.settings import Document

from app.collection.article_completion.content import RawResponse, ScrapedContent
from app.collection.article_completion.errors import (
    ArticleContentTypeError,
    ArticleExtractionCrashedError,
    ArticleExtractionCrashReason,
    ArticleExtractionEmptyError,
)
from app.collection.article_completion.html_extraction import extract_html_content


@pytest.fixture
def raw() -> RawResponse:
    """抽出器の結果を差し替えるテスト用のHTML応答。"""
    return RawResponse(
        url="https://example.com/extraction",
        content_type="text/html",
        charset_from_header=None,
        content=b"<html></html>",
        decoded_text="<html></html>",
    )


@pytest.mark.parametrize("encoding", ["utf-8", "shift_jis"])
def test_extracts_material_from_real_html(encoding: str) -> None:
    """実HTMLを指定文字コードで読み、タイトルと本文を抽出する。"""
    title = f"新しい観測衛星の報告 {encoding}"
    body = (
        f"観測装置の試験報告書には識別子 {encoding} が記録された。"
        "研究チームは新しい観測衛星から届いた地表の画像を公開した。"
        "今回の観測では雲の移動や海面の温度変化を継続的に測定し、"
        "気象予測の精度向上に役立つデータを収集している。"
        "得られた結果は今後の観測計画にも反映される予定だ。"
    )
    html = (
        f'<html><head><meta charset="{encoding}"><title>{title}</title></head>'
        f"<body><article><h1>{title}</h1><p>{body}</p></article></body></html>"
    )
    content = html.encode(encoding)
    raw = RawResponse(
        url=f"https://example.com/satellite-{encoding}",
        content_type=" Text/HTML ; charset=" + encoding,
        charset_from_header=None,
        content=content,
        decoded_text=content.decode("utf-8", errors="replace"),
    )
    result = extract_html_content(raw)
    assert result.title == title
    assert body in result.body


@pytest.mark.parametrize(
    ("title", "text", "expected_title", "expected_body"),
    [(None, " Short ", None, "Short"), (" <b> </b> ", None, None, "")],
)
def test_returns_partial_material(
    raw, title, text, expected_title, expected_body
) -> None:
    """項目が不足したDocumentも整形した素材として返す。"""
    with patch(
        "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
        return_value=Document(title=title, text=text, date=None),
    ):
        result = extract_html_content(raw)
    assert result == ScrapedContent(
        title=expected_title, body=expected_body, published_at=None
    )


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "application/json",
        "application/pdf",
        "application/xhtml+xml",
        "application/text/html",
        "text/html-invalid",
    ],
)
def test_rejects_non_html_with_original_content_type(content_type) -> None:
    """HTML以外は元のContent-Typeを保持して入力段階で拒否する。"""
    raw = RawResponse(
        url="https://example.com/document",
        content_type=content_type,
        charset_from_header=None,
        content=b"{}",
        decoded_text="{}",
    )
    with pytest.raises(ArticleContentTypeError) as caught:
        extract_html_content(raw)
    assert caught.value.content_type == content_type


def test_no_result_is_extraction_empty(raw) -> None:
    """抽出結果なしを抽出器の異常と区別する。"""
    with (
        patch(
            "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
            return_value=None,
        ),
        pytest.raises(ArticleExtractionEmptyError),
    ):
        extract_html_content(raw)


def test_unexpected_result_is_extraction_crash(raw) -> None:
    """Document以外の結果は想定外結果として伝える。"""
    with (
        patch(
            "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
            return_value={"text": "body"},
        ),
        pytest.raises(ArticleExtractionCrashedError) as caught,
    ):
        extract_html_content(raw)
    assert caught.value.reason == ArticleExtractionCrashReason.UNEXPECTED_RESULT


def test_parser_exception_preserves_cause(raw) -> None:
    """抽出器の通常例外は原因を保って抽出異常に変換する。"""
    original = ValueError("extraction failed")
    with (
        patch(
            "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
            side_effect=original,
        ),
        pytest.raises(ArticleExtractionCrashedError) as caught,
    ):
        extract_html_content(raw)
    assert caught.value.reason == ArticleExtractionCrashReason.EXCEPTION
    assert caught.value.__cause__ is original


@pytest.mark.parametrize("original", [asyncio.CancelledError(), SystemExit(1)])
def test_cancellation_and_exit_propagate(raw, original) -> None:
    """キャンセルとプロセス終了は抽出失敗に分類しない。"""
    with (
        patch(
            "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
            side_effect=original,
        ),
        pytest.raises(type(original)) as caught,
    ):
        extract_html_content(raw)
    assert caught.value is original


def test_material_conversion_exception_propagates(raw) -> None:
    """抽出器の呼び出し外で起きた例外は抽出異常に変換しない。"""
    original = RuntimeError("material conversion failed")
    with (
        patch(
            "app.collection.article_completion.html_extraction.trafilatura.bare_extraction",
            return_value=Document(title="Title", text="body", date=None),
        ),
        patch.object(ScrapedContent, "from_extraction", side_effect=original),
        pytest.raises(RuntimeError) as caught,
    ):
        extract_html_content(raw)
    assert caught.value is original
