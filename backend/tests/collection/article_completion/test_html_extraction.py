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


def _history_article(slug: str, *paragraphs: str) -> RawResponse:
    html = (
        f"<html><head><title>{slug}</title></head><body><article>"
        + "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
        + "</article></body></html>"
    )
    return RawResponse(
        url=f"https://example.com/{slug}",
        content_type="text/html",
        charset_from_header="utf-8",
        content=html.encode(),
        decoded_text=html,
    )


def test_repeated_extraction_preserves_material() -> None:
    """同じHTMLを繰り返し抽出しても、素材は前回の処理で欠落しない。"""
    body = (
        "再配送の観測試験では、新しい気象衛星から届いた画像を解析した。"
        "研究チームは雲の分布と地表の温度を複数の地点で測定し、"
        "従来の予測モデルとの違いを報告書にまとめて公開している。"
        "観測装置の校正方法も記載されており、各地の研究機関が同じ条件で"
        "結果を比較できるよう、測定データと分析手順を提供する予定だ。"
    )
    raw = _history_article("repeated-observation", body)
    first = extract_html_content(raw)
    assert body in first.body

    for _ in range(5):
        assert extract_html_content(raw) == first


def test_previous_articles_do_not_remove_shared_paragraph() -> None:
    """別記事に含まれた共通の段落も、今回の記事の素材として残す。"""
    shared = (
        "共同観測計画では各研究機関が取得したデータを共通の形式で公開する。"
        "測定機器の精度や設置場所の違いを確認できるよう、校正記録と観測条件も"
        "添付される。利用者は各地点の気温や降水量を比較し、長期的な気候変動の"
        "傾向を調べることができる。公開された資料は教育活動にも利用できる。"
    )
    raw = _history_article("shared-observation", shared)
    first = extract_html_content(raw)
    assert shared in first.body

    for region in ("北海道", "東北", "関東", "九州", "沖縄"):
        local_report = (
            f"{region}の観測拠点では今年度の測定計画を発表した。"
            "新たな観測機器を設置し、過去の測定値との比較を進めている。"
            "担当者は現地の環境条件を確認しながら機器の動作を検証している。"
            "今後は観測地点を増やし、地域内での気候の違いについても"
            "詳しいデータを集めていく予定だ。調査の成果は年度末に公開される。"
        )
        extract_html_content(_history_article(region, shared, local_report))

    assert extract_html_content(raw) == first
