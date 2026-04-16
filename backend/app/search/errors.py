"""Search ドメインのエラー定義。"""


class SearchError(Exception):
    """検索処理が失敗したときに送出される(例: embedding 生成失敗)。"""
