"""Fetcher 基底クラス群 — 構造同型を持つソース集合の共通実装。

per-source 実装が ``Fetcher`` Protocol を満たすことが第一の要件であり、本
パッケージは Protocol 適合済の共通実装を「再利用可能なコンポーネント」と
して提供するに留まる。Protocol を継承クラスに置き換える意図はない (memory
``feedback_responsibility_by_purpose.md``)。
"""
