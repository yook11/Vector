"""Source レジストリ周辺の補助 (composition root 隣接)。

``fetchers/strategy.py`` の ``SOURCES`` を ACL (repository) から隠す seam を
提供する。repository は本パッケージの Protocol にのみ依存し、composition root
の ``SOURCES`` を import しない (spec §4.6 ガードレール 1)。
"""
