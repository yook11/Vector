"""Shared security primitives。

横断的なセキュリティ判定 (SSRF 防御, IP 範囲ポリシー等) の SSoT。
特定 BC やフェッチ層の都合を持ち込まないこと: ここで決まる
ポリシーを各層が呼び出して使う。
"""
